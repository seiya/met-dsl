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
import shutil
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
    "command_log.jsonl", "stdout.log", "stderr.log",
    "compile.stdout.log", "compile.stderr.log",
})

# Deterministic in-process build/run capture limit. The canonical per-step
# stdout/stderr log files must be FULL (untrimmed); the MCP `_run_command` trims its
# returned stdout/stderr to this byte budget, so we pass a large value to avoid losing
# detail (e.g. a big compiler error dump). The runner writes its data to JSON files,
# not stdout, so program stdout/stderr are normally tiny regardless.
_FULL_CAPTURE_LIMIT: int = 50_000_000


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

# C2 backstop: after this many consecutive execute (no-verdict) failures on a node, a
# Generate restart is deemed unable to fix the (IR-rooted) structural mismatch, so the
# defect is reattributed to the IR and Compile is reopened instead of looping Generate.
# Kept < MAX_ATTEMPTS_PER_PHASE so the escalation fires within the attempt budget.
C2_EXECUTE_FAIL_ESCALATION_THRESHOLD = 2


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
    evidence_artifacts: tuple[str, ...] = ("state_snapshots",),
    exe_name: str | None = None,
    makefile_host_authored: bool = False,
    repair: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Construct the record-launch --request-json payload for one substep.

    case_ids is required for validate.execute (per-case raw/state_snapshots paths).
    evidence_artifacts is the IR's required raw-evidence artifact types (validate.execute
    only); it drives which raw/* paths are deliverables so an IR that does not require
    state_snapshots is not forced to produce them (phase_04 §44).
    repair carries issue_severity/repair_strategy/repair_target_agent_run_id/
    repair_reason on a retry (defaults to the literal "none" the templates use).
    """
    spec = refs.spec_path
    skill = _skill_name(step, substep)
    role = child_agent_role(step)
    # Build and Validate.execute run in-process (no leaf), so they carry no skill /
    # leaf prompt — only the bookkeeping the capability/phase_state need.
    deterministic = step == "build" or (step == "validate" and substep == "execute")
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
    }
    if deterministic:
        req["deterministic"] = True
    else:
        req["skill_name"] = skill
        req["skill_ref"] = f"skills/{skill}/SKILL.md"
    if substep is not None:
        req["substep"] = substep

    must_read: list[str] = ([] if deterministic
                            else [f"skills/{skill}/SKILL.md", *_DOC_CORE, _PHASE_DOC[step]])

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
        # For any make+fortran node (leaf or dependency) the conductor authors src/Makefile
        # host-side (_write_makefile), so it is NOT a leaf output — omit it from
        # allowed_output_paths (and required outputs) exactly like lineage.json. c/cpp/mixed
        # keep LLM authoring, so the leaf still lists it there.
        make_entry = [] if makefile_host_authored else [f"{src}/src/Makefile"]
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
                *make_entry,
                f"{src}/src/command_log.jsonl",
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
                *make_entry,
                f"{src}/src/command_log.jsonl",
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
        # The binary basename is the Makefile's BIN (resolved by the conductor and passed
        # in); fall back to the <spec_id>_runner name when unknown (e.g. unit fixtures).
        req["allowed_output_paths"] = [
            f"{bdir}/bin/{exe_name or (refs.spec_id + '_runner')}",
            f"{bdir}/binary_meta.json",
            f"{bdir}/command_log.jsonl",
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
                f"{rundir}/command_log.jsonl",
                f"{rundir}/diagnostics.json",
                f"{rundir}/perf.json",
                f"{rundir}/trial_meta.json",
                f"{rundir}/quality_check.json",
                f"{rundir}/raw/metrics_basis.json",
            ]
            # Raw-evidence deliverables are IR-driven (phase_04 §44): only require the
            # artifacts the IR's required_evidence declares.
            if "state_snapshots" in evidence_artifacts:
                for cid in case_ids:
                    outs.append(f"{rundir}/raw/state_snapshots/{cid}.json")
                outs.append(f"{rundir}/raw/state_snapshots/snapshot_schema.json")
            if "execution_trace.json" in evidence_artifacts:
                outs.append(f"{rundir}/raw/execution_trace.json")
            outs += [
                f"{rundir}/stdout.log",
                f"{rundir}/stderr.log",
                f"{refs.source_dir()}/src/command_log.jsonl",
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

    # Deterministic steps have no leaf, so no skill_must_read_refs (the conductor reads
    # what it needs in-process; the read_manifest is irrelevant for them).
    req["skill_must_read_refs"] = "" if deterministic else ",".join(must_read)
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
    # Unpinned spec-side alias (never a pinned version — that would go stale as
    # versions update). The EXACT version each leaf actually ran is resolved from
    # its session transcript and recorded onto its agent_runs row in _agent_run_json.
    agent_model: str = "opus"
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

    def _ensure_codex_feature_cache(self) -> None:
        """Host-side: probe the codex hooks feature ONCE per orchestration and persist the
        result to the leaf-unwritable cache (orchestration-dir root, RO inside the bwrap
        sandbox), so the in-sandbox codex hook reads a host-certified value it cannot
        forge. No-op for non-codex backends and after the first call (memoized). The probe
        runs the SAME command prefix the leaf runs (`leaf_command`'s `base` — a custom
        `--llm-command` wrapper, else the bare backend), so it certifies the executable the
        leaf will actually use, not a hardcoded `codex`. A leaf can never write this cache
        (the prior design wrote it from the in-sandbox hook into the leaf-writable hooks/
        dir).

        Fails closed when the feature is NOT certified (hooks disabled or the probe errored)
        and the requirement is on: a codex leaf whose PreToolUse/PostToolUse file-access
        hooks would not fire must not launch at all — the in-sandbox gate fail-closes only if
        the hook actually runs, which it does not when the hooks feature is off, so recording
        a disabled cache without blocking would leave the leaf unguarded by the hook layer.
        Honours the same `METDSL_REQUIRE_CODEX_HOOKS_FEATURE` opt-out the hook does."""
        if self.backend != "codex":
            return
        if getattr(self, "_codex_feature_cache_written", False):
            return
        from tools.hooks.codex_feature import probe_and_write_codex_feature_cache
        # Mirror leaf_command()/_readonly_sandbox_profile: the leaf's invocation prefix is
        # the parsed llm_command (with any wrapper flags), else the bare backend.
        command = shlex.split(self.llm_command) if self.llm_command.strip() else [self.backend]
        enabled, detail = probe_and_write_codex_feature_cache(
            repo_root=self.repo_root, orchestration_id=self.orchestration_id,
            command=command or [self.backend])
        # Read the requirement from self.env (the same env the leaf's hook inherits via
        # _child_env), defaulting to required — matches the hook's gate semantics.
        require_raw = self.env.get("METDSL_REQUIRE_CODEX_HOOKS_FEATURE", "1").strip().lower()
        hooks_required = require_raw not in {"0", "false", "no"}
        if hooks_required and not enabled:
            # Fail closed BEFORE memoizing, so this never degrades into an allow on a retry.
            raise SandboxEnforcementError(
                f"codex hooks feature not certified for orchestration "
                f"{self.orchestration_id} ({detail}); refusing to launch a codex leaf whose "
                "file-access hooks would not fire (fail-closed)")
        self._codex_feature_cache_written = True

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
        # Host-certify the codex hooks feature into the leaf-unwritable cache before the
        # codex leaf launches (the in-sandbox hook reads it read-only; see
        # _ensure_codex_feature_cache). Memoized; no-op for claude.
        self._ensure_codex_feature_cache()
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
        try:
            proc = subprocess.run(
                argv, cwd=self.repo_root, env=child_env, text=True, capture_output=True, check=False,
            )
        except FileNotFoundError as exc:
            # The leaf executable could not be found. Under mandatory bwrap argv[0] is
            # `bwrap`, so a missing binary means the host cannot sandbox the leaf at all
            # (e.g. the startup preflight was bypassed via
            # METDSL_ORCHESTRATION_ASSUME_BWRAP on a host where bwrap is absent — the
            # probe lied). Funnel it into the SAME fail-closed path as a missing/invalid
            # profile rather than letting a raw OSError bubble up as a generic
            # conductor_error: every "leaf cannot be sandboxed" condition terminalizes
            # consistently as a sandbox-enforcement failure.
            if self._bwrap_enabled():
                raise SandboxEnforcementError(
                    f"cannot launch sandboxed leaf — executable not found "
                    f"(bwrap missing on this host?): {exc}") from exc
            raise
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

    def _is_leaf_node(self, refs: NodeRefs) -> bool:
        """A node whose `dependency.direct_deps` is explicitly present and empty. An absent
        dependency block / absent direct_deps returns False (undeterminable -> treat as
        non-leaf, matching the runtime's `_impl_is_leaf_node` which returns None there).

        NOTE: leaf-ness no longer gates `src/Makefile` authorship — the conductor authors it
        for every make+fortran node (leaf OR dependency; see `_conductor_authors_makefile` /
        `_write_makefile`'s Model B branch). Retained as the canonical leaf predicate for the
        leaf concept itself (and its agreement with `_impl_is_leaf_node`)."""
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        dep = ir.get("dependency") if isinstance(ir, dict) else None
        if not isinstance(dep, dict) or "direct_deps" not in dep:
            return False
        return not dep.get("direct_deps")

    def _conductor_authors_makefile(self, refs: NodeRefs) -> bool:
        """The conductor authors `src/Makefile` iff build_system=make AND language=fortran —
        exactly the scope of `_write_makefile`, for BOTH leaf and dependency nodes. The
        dependency Makefile is as IR-determined as the leaf one (the closure + per-dep object
        rules come from `dependency.transitive_deps`/`all_nodes`; Model B), so the conductor
        authors it too and the generate leaf must not. Single source of truth for the live
        author call AND the write-authorization removal, so they cannot disagree (which would
        orphan the Makefile, or leave it double-owned). c/cpp/mixed keep LLM authoring."""
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        impl = (ir.get("impl_defaults") or {}) if isinstance(ir, dict) else {}
        tc = (impl.get("toolchain") or {}) if isinstance(impl, dict) else {}
        return (str(tc.get("build_system") or "make").lower() == "make"
                and str(tc.get("language") or "fortran").lower() == "fortran")

    def _dependency_closure_nodes(self, refs: NodeRefs) -> list[str]:
        """Dependency node_keys in compile order (deepest first) from the IR closure.

        The build closure is the **union** of `dependency.direct_deps[]` and
        `dependency.transitive_deps[]` (per the compile V4 contract, phase_01 §V4: their union
        matches `all_nodes`). `transitive_deps` lists only the INDIRECT deps reached `via` an
        intermediate, so a one-hop dependency (e.g. `top -> base` with `base` a leaf) has a
        non-empty `direct_deps` and an empty `transitive_deps` — reading only `transitive_deps`
        would resolve the closure empty and wrongly block the build. Deduped (first occurrence
        wins) and ordered by `dependency.all_nodes[].topo_level` ascending (deepest deps, which
        provide modules the shallower ones `use`, compile first). The node_keys carry the
        resolved `@<version>`, so the staging path (`_stage_dependency_sources`) and the
        Makefile object names (`_dependency_closure` -> spec_ids) derive from a single ordered
        list and cannot disagree on which dep / which version. The spec_id basenames must
        nonetheless be unique across the closure (the staged `<spec_id>_model.f90` / object
        rules are keyed on the bare spec_id); a same-spec_id clash (diamond) raises here (L6)."""
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        dep = (ir.get("dependency") or {}) if isinstance(ir, dict) else {}
        levels: dict[str, int] = {}
        for n in dep.get("all_nodes") or []:
            if isinstance(n, dict) and isinstance(n.get("node_key"), str):
                levels[n["node_key"]] = n.get("topo_level") or 0
        closure: list[str] = []
        seen: set[str] = set()
        for list_key in ("direct_deps", "transitive_deps"):
            for d in dep.get(list_key) or []:
                nk = d.get("node_key") if isinstance(d, dict) else d
                if isinstance(nk, str) and nk.strip() and nk != refs.node_key and nk not in seen:
                    seen.add(nk)
                    closure.append(nk)
        closure.sort(key=lambda nk: levels.get(nk, 0))
        # L6 guard: the Model B staged source basename (`<spec_id>_model.f90`) and the
        # Makefile object rules (`$(OBJDIR)/<spec_id>_model.o`) are keyed on the bare
        # spec_id (kind/@version dropped), and the dep's generated source declares a Fortran
        # `module <spec_id>_model`. Two distinct closure node_keys sharing a spec_id (a
        # diamond: `component/foo@1.0.0` + `component/foo@2.0.0`, or `component/foo` +
        # `model/foo`) would silently clobber each other (last-write-wins stage + duplicate
        # `.o` rules + a duplicate module). Version-qualifying the basename alone would not
        # fix the module-name clash, so fail closed with an actionable cause until proper
        # multi-version support (module renaming) lands.
        by_sid: dict[str, list[str]] = {}
        for nk in closure:
            by_sid.setdefault(spec_id_of(nk), []).append(nk)
        clashes = {sid: nks for sid, nks in by_sid.items() if len(nks) > 1}
        if clashes:
            raise RuntimeError(
                f"dependency closure for {refs.node_key} has spec_id basename collisions "
                f"{clashes}: the Model B staged source `<spec_id>_model.f90` and Makefile "
                f"`<spec_id>_model.o`/`module <spec_id>_model` are keyed on the bare spec_id, "
                f"so two deps sharing a spec_id (differing version/kind) would clobber each "
                f"other. Version-qualify the object/staged/module basenames before allowing "
                f"multi-version/diamond closures (deterministic_followups.md L6).")
        return closure

    def _dependency_closure(self, refs: NodeRefs) -> list[str]:
        """Dependency spec_ids in compile order (deepest first) — the `<dep>_model.o`/`.f90`
        basenames the deterministic dependency Makefile (`_write_makefile` non-leaf branch)
        compiles + links. Derived from `_dependency_closure_nodes` so the Makefile object
        names and the staged source filenames stay in lockstep."""
        return [spec_id_of(nk) for nk in self._dependency_closure_nodes(refs)]

    def _write_makefile(self, refs: NodeRefs) -> None:
        """Author the `src/Makefile` host-side (runtime-owned), deterministically.

        For a leaf node (no dependencies) the Makefile is a pure function of the IR: the
        pinned `<spec_id>_model/runner.f90` names, the fixed runner->model `use`-graph, and
        the structured `impl_defaults.toolchain`/`target` flags. Authoring it here removes a
        class of generate regenerate-loops (Makefile-shape failures) and the long Makefile
        contract the generate leaf would otherwise internalize, and makes the build
        reproducible. Mirrors `_write_lineage` (runtime-owned artifact). Scoped to
        build_system=make + language=fortran; c/cpp/mixed fall back to LLM authoring. The
        post_generate validators still run against this file as a safety net.

        Imposes `BIN ?= <spec_id>_runner` (overridable so Build/Validate.execute can pin the
        canonical binary name) and FFLAGS derived from toolchain.standard + target.backend.

        A non-empty dependency closure (Model B, docs/design) emits per-dep object rules +
        a `DEP_OBJS` link list; the conductor stages each `<dep>_model.f90` into `$(OBJDIR)`
        before `make` (`_stage_dependency_sources`, called from `_build_inproc`). `run_phase`
        authors this for every make+fortran node (leaf or dependency) — see
        `_conductor_authors_makefile`.
        """
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        impl = (ir.get("impl_defaults") or {}) if isinstance(ir, dict) else {}
        tc = (impl.get("toolchain") or {}) if isinstance(impl, dict) else {}
        language = str(tc.get("language") or "fortran").lower()
        standard = str(tc.get("standard") or "f2008")
        build_system = str(tc.get("build_system") or "make").lower()
        if build_system != "make" or language != "fortran":
            return  # c/cpp/mixed (or non-make) keep LLM authoring — out of scope.
        target = (impl.get("target") or {}) if isinstance(impl, dict) else {}
        backend = str(target.get("backend") or "").lower()

        model = f"{refs.spec_id}_model"
        runner = f"{refs.spec_id}_runner"
        exe = self._resolve_exe_name(refs)  # canonical <spec_id>_runner
        flags = f"-std={standard} -O2"
        if backend == "openmp":
            flags += " -fopenmp"
        flags += " -J$(OBJDIR) -I$(OBJDIR)"

        # Dependency closure (Model B). Empty for leaf nodes -> the blocks
        # below collapse to "" and the leaf template is emitted byte-for-byte.
        closure = self._dependency_closure(refs)
        dep_objs_line = ""
        dep_rules = ""
        model_dep_prereq = ""
        link_dep_prereq = ""
        if closure:
            dep_objs = " ".join(f"$(OBJDIR)/{d}_model.o" for d in closure)
            dep_objs_line = f"\nDEP_OBJS = {dep_objs}\n"
            model_dep_prereq = " $(DEP_OBJS)"
            link_dep_prereq = "$(DEP_OBJS) "
            # Deepest-first: each dep object depends on all deeper dep objects so their
            # `.mod` exist first (conservative over-ordering — safe for correctness). The
            # conductor stages `<dep>_model.f90` into $(OBJDIR) before make.
            parts = []
            for i, d in enumerate(closure):
                deeper = " ".join(f"$(OBJDIR)/{closure[j]}_model.o" for j in range(i))
                deeper = (deeper + " ") if deeper else ""
                parts.append(
                    f"$(OBJDIR)/{d}_model.o: $(OBJDIR)/{d}_model.f90 {deeper}| $(OBJDIR)\n"
                    f"\t$(FC) $(FFLAGS) -c $(OBJDIR)/{d}_model.f90 -o $(OBJDIR)/{d}_model.o\n")
            dep_rules = "\n" + "\n".join(parts)

        template = f"""\
# Deterministic Makefile authored by the conductor (build_system=make, language=fortran).
# Out-of-source capable: OBJDIR/BINDIR/RUNDIR default to "." and are overridden by
# Build (compile_project) and Validate.execute (run_quality_checks).

# FC is pinned with := (not ?=): make ships a built-in FC=f77 (origin default), and ?= does
# NOT override a default-origin variable, so `FC ?= gfortran` would silently leave FC=f77.
# The dirs/BIN stay ?= because Build/Validate.execute inject them via command line / env.
FC      := gfortran
OBJDIR  ?= .
BINDIR  ?= .
RUNDIR  ?= .
FFLAGS  ?= {flags}

BIN ?= {exe}

MODEL_SRC  = {model}.f90
RUNNER_SRC = {runner}.f90

MODEL_OBJ  = $(OBJDIR)/{model}.o
RUNNER_OBJ = $(OBJDIR)/{runner}.o
{dep_objs_line}
.PHONY: all test clean
.DEFAULT_GOAL := all

all: $(BINDIR)/$(BIN)
{dep_rules}
$(MODEL_OBJ): $(MODEL_SRC){model_dep_prereq} | $(OBJDIR)
\t$(FC) $(FFLAGS) -c $(MODEL_SRC) -o $(MODEL_OBJ)

$(RUNNER_OBJ): $(RUNNER_SRC) $(MODEL_OBJ) | $(OBJDIR)
\t$(FC) $(FFLAGS) -c $(RUNNER_SRC) -o $(RUNNER_OBJ)

$(BINDIR)/$(BIN): {link_dep_prereq}$(MODEL_OBJ) $(RUNNER_OBJ) | $(BINDIR)
\t$(FC) $(FFLAGS) {link_dep_prereq}$(MODEL_OBJ) $(RUNNER_OBJ) -o $(BINDIR)/$(BIN)

# $(sort ...) dedups the target list: when OBJDIR==BINDIR (in-source make, both ".")
# it collapses to a single target, avoiding the harmless `target '.' given more than
# once` warning (and without two recipes for the same target).
$(sort $(OBJDIR) $(BINDIR)):
\tmkdir -p $@

test:
\ttest -x $(BINDIR)/$(BIN) || {{ echo "error: $(BINDIR)/$(BIN) not built; run 'make all' first" >&2; exit 1; }}
\tmkdir -p $(RUNDIR)/raw/state_snapshots
\tcd $(RUNDIR) && $(BINDIR)/$(BIN)

clean:
\trm -f $(OBJDIR)/*.o $(OBJDIR)/*.mod $(BINDIR)/$(BIN)
"""
        path = self.repo_root / refs.source_dir() / "src" / "Makefile"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template, encoding="utf-8")

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

    def _add_superseded_run_ids(self, run_ids: list[str], reason: str) -> dict[str, Any]:
        """Tombstone substep arids of a phase attempt that fail-closed on a leaf transport
        error (it wrote no step_result), so a later --resume can reach pass: the orphaned
        terminalized substeps are exempted from the completion vouch (see runtime
        add_superseded_run_ids). No-op caller-side when run_ids is empty."""
        return self.runtime([
            "add-superseded-runs", *self._oid_args(),
            "--reason", reason, "--run-ids", *run_ids,
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
        # strip() so the case_id identity is the SAME across the runner argv
        # (--cases), the expected raw/state_snapshots/<case_id>.json deliverable
        # path, and the validator's _case_ids_for_execution exemption set.
        return tuple(sorted(
            c["case_id"].strip() for c in tcs
            if isinstance(c, dict) and isinstance(c.get("case_id"), str)
            and c["case_id"].strip()
        ))

    def _read_evidence_artifacts(self, refs: NodeRefs) -> tuple[str, ...]:
        """IR-declared required raw-evidence artifact types for validate.execute
        allowed_output_paths. Returns the IR's actual artifacts with NO fallback so the
        deliverable set stays identical to what `_promote_run_evidence` /
        `_author_snapshot_schema` (which read the same `_required_evidence_artifacts`)
        produce — a fallback here would require evidence the promoter never creates
        (fail-closed) and violate phase_04 §44 for IRs that declare no state_snapshots."""
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        return tuple(self._required_evidence_artifacts(ir))

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

        def _fresh_deliverables_written(paths: list[str]) -> bool:
            # All DELIVERABLE outputs (excluding the audit/process logs whose placement
            # varies by build system) exist AND were authored in this attempt
            # (mtime >= min_mtime), so a retry/reopen never passes on stale files.
            required = [p for p in paths if Path(p).name not in _OPTIONAL_OUTPUT_BASENAMES]
            present = [p for p in required if (self.repo_root / p).exists()]
            if len(present) != len(required):
                return False
            return all((self.repo_root / p).stat().st_mtime >= min_mtime for p in present)

        if phase == "compile" and substep == "verify":
            meta = _read_json(self.repo_root / refs.ir_ref / "ir_meta.json") or {}
            status = "pass" if meta.get("verification_status") == "pass" else "fail"
        elif phase == "generate" and substep == "verify":
            meta = _read_json(self.repo_root / refs.source_dir() / "source_meta.json") or {}
            status = "pass" if meta.get("verification_status") == "pass" else "fail"
        elif phase == "validate" and substep == "judge":
            agg = _read_json(self.repo_root / refs.run_node_dir() / "aggregate_verdict.json") or {}
            status = "pass" if str(agg.get("aggregate_verdict") or agg.get("overall")) in ("pass", "xfail") else "fail"
        elif phase == "build":
            # Deterministic build: the conductor-authored binary_meta records the compile
            # + post_build-gate verdict. A content failure (compile/link error, post_build
            # violation) is verification_status=fail with rc 0, so the substep fails here
            # and classify_build_failure routes it to Generate (not transport fail_closed).
            meta = _read_json(self.repo_root / refs.binary_dir() / "binary_meta.json") or {}
            status = "pass" if (meta.get("verification_status") == "pass"
                                and _fresh_deliverables_written(allowed_output_paths)) else "fail"
        elif phase == "validate" and substep == "execute":
            # Deterministic execute: trial_meta.status reflects run_program +
            # quality_check + post_execute gate; content failures (rc 0) route via the
            # validate tables / diagnostician. A run_program runtime error writes no
            # trial_meta, so the missing-status read fails the substep here too.
            meta = _read_json(self.repo_root / refs.run_node_dir() / "trial_meta.json") or {}
            status = "pass" if (meta.get("status") == "pass"
                                and _fresh_deliverables_written(allowed_output_paths)) else "fail"
        else:
            # remaining producing substeps (compile.generate / generate.generate): pass
            # only when ALL DELIVERABLE outputs were written this attempt (mtime guard);
            # the audit/process logs (optional basenames) are excluded. The downstream
            # verify certifies the content.
            status = "pass" if _fresh_deliverables_written(allowed_output_paths) else "fail"
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
        # Record the EXACT model the leaf actually ran, resolved from its own session
        # transcript (the leaf's session id == child_arid, pinned via --session-id at
        # launch). This is the runtime-resolved ground truth that replaces the unpinned
        # alias carried in the launch request — record_agent_run only setdefaults the
        # alias, so a value set here wins. Claude only; a codex leaf's transcript lives
        # outside ~/.claude, so it keeps the launch-request alias. If unresolvable
        # (no transcript yet, or a leaf that crashed before any assistant message),
        # we leave agent_model absent and let record_agent_run backfill the alias.
        if self.backend == "claude":
            from tools.orchestration_runtime import resolve_claude_model_from_transcript
            resolved = resolve_claude_model_from_transcript(child_arid)
            if resolved:
                payload["agent_model"] = resolved
        if status == "pass":
            payload["output_refs"] = output_refs
        elif result_summary and result_summary.strip():
            # A failed substep carries no output_refs, so _validate_agent_summary_text
            # requires a summary/reason token; surface the leaf failure reason here so
            # finalize-child produces a valid agent.summary.txt instead of crashing.
            payload["result_summary"] = result_summary.strip()
        return payload

    # -- deterministic (non-LLM) substep execution ----------------------------
    # Build and Validate.execute are contractually non-LLM (deterministic compile /
    # run), so the conductor ALWAYS runs their body IN-PROCESS (no `claude -p` leaf) by
    # calling the build-runtime MCP tool handlers directly. Validate.judge stays an LLM
    # leaf (its independent semantic check is essential).

    @staticmethod
    def _is_deterministic_substep(phase: str, substep: str | None) -> bool:
        return phase == "build" or (phase == "validate" and substep == "execute")

    def _capability_token(self, child_arid: str) -> str:
        path = (self.repo_root / "workspace" / "orchestrations" / self.orchestration_id
                / "capabilities" / f"{child_arid}.json")
        cap = _read_json(path) or {}
        token = str(cap.get("capability_token", "")).strip()
        if not token:
            raise RuntimeError(f"deterministic step: missing capability_token at {path}")
        return token

    def _resolve_exe_name(self, refs: NodeRefs) -> str:
        """The canonical execution binary basename: `<spec_id>_runner`.

        Build and Validate.execute IMPOSE this name on the Makefile (Build via the make
        command line, Validate.execute via the make_test environment — which requires the
        Makefile's `BIN ?=` overridable form, enforced by post_generate). The binary name
        is thus deterministic and consistent with the runner source/program names, instead
        of varying with whatever default `BIN` the generator chose."""
        return f"{refs.spec_id}_runner"

    @staticmethod
    def _require_make_build_system(build_system: str, phase: str) -> None:
        """The in-process deterministic bodies hard-code the in-source Make layout
        (OBJDIR/BINDIR/RUNDIR overrides, make_test preset, binary under binary/<id>/bin,
        Make command-log placement). Non-Make toolchains (cmake/meson/ninja) would be
        silently misplaced, so fail loudly until in-process support is implemented for
        them. All current specs are build_system=make."""
        if str(build_system).strip().lower() != "make":
            raise RuntimeError(
                f"deterministic in-process {phase} supports build_system=make only "
                f"(got {build_system!r}); non-Make toolchains are not implemented for the "
                f"in-process path")

    @staticmethod
    def _classify_build_failure_category(return_code: int, stderr: str) -> str:
        """Mechanical classification per phase_03_build.md (no LLM)."""
        s = (stderr or "").lower()
        if "no rule to make target" in s:
            return "make_error"
        if "undefined reference" in s or "unresolved external" in s:
            return "link_error"
        return "compile_error"

    @staticmethod
    def _extract_failure_source_refs(stderr: str, src_ref: str) -> list[str]:
        """Source paths the compiler/linker named in its error output, rebased under
        the canonical `<src_ref>` so Generate can target only the offending files
        (phase_03 retry trigger). Best-effort: empty when nothing parseable."""
        names: set[str] = set()
        for m in re.finditer(r"([\w./-]+\.(?:f90|f95|f|c|cc|cxx|cpp|h|hpp))",
                             stderr or "", re.IGNORECASE):
            names.add(Path(m.group(1)).name)
        return sorted(f"{src_ref}/{n}" for n in names)

    def _run_deterministic_substep(self, refs: NodeRefs, phase: str, substep: str | None,
                                   child_arid: str, request: dict[str, Any]) -> ProcResult:
        """Run a non-LLM substep body in-process and return a ProcResult shaped like a
        leaf's (returncode 0 == clean conductor run; a content failure such as a
        compile error is still rc 0 and routed via binary_meta.failure_category).
        A nonzero rc means a conductor-side/MCP-gate failure -> transport fail_closed."""
        try:
            cap_token = self._capability_token(child_arid)
            if phase == "build":
                out = self._build_inproc(refs, child_arid, cap_token)
            elif phase == "validate" and substep == "execute":
                out = self._execute_inproc(refs, child_arid, cap_token)
            else:
                raise RuntimeError(f"no deterministic body for {phase}.{substep}")
        except Exception as exc:  # noqa: BLE001 - surfaced as transport failure
            return ProcResult(1, "", f"deterministic_{phase}_error: {exc}")
        return ProcResult(int(out.get("returncode", 0)), out.get("stdout", ""), out.get("stderr", ""))

    def _stage_dependency_sources(self, refs: NodeRefs, obj_dir: Path) -> list[str]:
        """Model B (docs/design): stage each dependency-closure `<dep>_model.f90` into the
        per-run build tmp `$(OBJDIR)` so the conductor-authored dependency Makefile
        (`_write_makefile` non-leaf branch) compiles + links the closure. Never touches the
        canonical `src/` — phase_02 §41 carve-out: a transient `$(OBJDIR)` stage is not a
        canonical-tree copy, so it is not the forbidden dependency mix-in.

        Each dep's model source is resolved from the dep's latest ready pipeline, then from the
        **certified binary** (`_latest_meta_under(.../binary/*/binary_meta.json)` — the same
        binary `_verify_dep_stage` certifies readiness against) via its `source_source_id` ->
        `source/<source_source_id>/src/<dep>_model.f90`. Binding to the certified binary's
        source (not the pipeline `lineage.json`, which tracks the latest *generated* source)
        guarantees the staged code is the exact source the ready binary/verdict was built from.
        node_keys carry `@<version>`, so the per-version workspace path is unambiguous.

        Returns the repo-relative refs of the staged sources (deepest-first). Raises on an
        unresolvable dependency: a missing dep source means the dependency was not built
        ready (run `--with-deps` first), which is a build precondition failure routed to
        transport fail_closed (operator --resume), NOT a content failure the generate retry
        loop could fix.

        No-op (returns []) unless the node is make ∧ fortran — staging is paired with the
        conductor-authored Fortran Makefile (`_write_makefile` non-leaf branch), which is the
        only consumer of the staged `<dep>_model.f90`. For a c/cpp/mixed dependency node the
        Generate child still owns the (LLM-authored) Makefile and its own dependency build, so
        the conductor must not stage Fortran sources (they do not exist under those names)."""
        from tools.orchestration_runtime import _latest_meta_under, _latest_pipeline_dir
        if not self._conductor_authors_makefile(refs):
            return []
        nodes = self._dependency_closure_nodes(refs)
        if not nodes:
            # Defense-in-depth: a genuine leaf has empty `direct_deps`. If `direct_deps` is
            # non-empty yet the closure (the direct+transitive union) still resolves empty, the
            # `direct_deps` entries have no resolvable `node_key` — a malformed IR violating the
            # compile closure contract (phase_01 §V4). The Makefile would have been authored
            # leaf-shaped (no DEP_OBJS) and the node's `use <dep>_model` would fail Build as a
            # missing-module compile error that misroutes to a Generate retry it cannot fix.
            # Fail closed here with a clear cause instead.
            ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
            dep = (ir.get("dependency") or {}) if isinstance(ir, dict) else {}
            if dep.get("direct_deps"):
                raise RuntimeError(
                    f"malformed IR for {refs.node_key}: dependency.direct_deps is non-empty "
                    f"but the build closure resolved empty (no resolvable node_key in "
                    f"direct_deps/transitive_deps); recompile (phase_01 §V4 closure contract)")
            return []
        obj_dir.mkdir(parents=True, exist_ok=True)
        staged: list[str] = []
        for nk in nodes:
            safe = node_key_safe(nk)
            sid = spec_id_of(nk)
            pipe_dir = _latest_pipeline_dir(
                self.repo_root / "workspace" / "pipelines" / safe)
            if pipe_dir is None:
                raise RuntimeError(
                    f"dependency {nk}: no ready pipeline under workspace/pipelines/{safe} "
                    f"to stage {sid}_model.f90 from (build the dependency closure first, "
                    f"e.g. run_workflow.py --with-deps)")
            # Bind the staged source to the SAME binary the readiness gate certified, NOT to
            # the pipeline-level lineage.json. `_verify_dep_stage` certifies the latest
            # `binary/*/binary_meta.json` (selected by id) and binds the aggregate_verdict to
            # it; that binary records the source it was actually built from in
            # `source_source_id`. The pipeline lineage.json, by contrast, tracks the latest
            # GENERATED source, which a Generate retry may have advanced past the certified
            # binary's source (newer source, not yet rebuilt/validated) — staging from lineage
            # would then compile the depending node against UNVERIFIED dependency code. Use the
            # certified binary's `source_source_id` so the staged source == the validated one.
            binary_meta_path = _latest_meta_under(pipe_dir, "binary/*/binary_meta.json")
            if binary_meta_path is None:
                raise RuntimeError(
                    f"dependency {nk}: no binary_meta.json under {self._rel(pipe_dir)} to "
                    f"resolve the certified source (dependency not built ready; "
                    f"run_workflow.py --with-deps first)")
            binary_meta = _read_json(binary_meta_path) or {}
            source_id = binary_meta.get("source_source_id")
            if not isinstance(source_id, str) or not source_id.strip():
                raise RuntimeError(
                    f"dependency {nk}: {self._rel(binary_meta_path)} has no source_source_id")
            model_src = pipe_dir / "source" / source_id / "src" / f"{sid}_model.f90"
            if not model_src.is_file():
                raise RuntimeError(
                    f"dependency {nk}: model source not found at {self._rel(model_src)}")
            shutil.copy2(model_src, obj_dir / f"{sid}_model.f90")
            staged.append(self._rel(model_src))
        return staged

    def _build_inproc(self, refs: NodeRefs, child_arid: str, cap_token: str) -> dict[str, str]:
        """Deterministic Build: in-process compile_project + binary_meta + post_build gate."""
        import sys as _sys
        mcp_dir = str(self.repo_root / "mcp_servers")
        if mcp_dir not in _sys.path:
            _sys.path.insert(0, mcp_dir)
        from build_runtime_server import tool_compile_project

        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        impl = (ir.get("impl_defaults") or {}) if isinstance(ir, dict) else {}
        toolchain = (impl.get("toolchain") or {}) if isinstance(impl, dict) else {}
        language = str(toolchain.get("language") or "fortran")
        build_system = str(toolchain.get("build_system") or "make")
        self._require_make_build_system(build_system, "build")

        src_dir = self.repo_root / refs.source_dir() / "src"
        bin_dir = self.repo_root / refs.binary_dir() / "bin"
        obj_dir = self.repo_root / "workspace" / "tmp" / child_arid / "build"
        exe = self._resolve_exe_name(refs)
        # DEPENDENCY BUILD (Model B, docs/design): for a make∧fortran node with dependencies,
        # stage each closure `<dep>_model.f90` into obj_dir ($(OBJDIR)) BEFORE compile, so the
        # conductor-authored dependency Makefile (_write_makefile non-leaf branch) compiles +
        # links the closure. Self-gated: a no-op for a leaf node (empty closure) and for
        # c/cpp/mixed nodes (LLM-authored Makefile owns its own dependency build). A staging
        # failure raises -> _run_deterministic_substep catches it as a transport fail_closed
        # (build precondition: the dependency must be built ready first). The transient OBJDIR
        # stage never touches canonical src/ (phase_02 §41 carve-out).
        self._stage_dependency_sources(refs, obj_dir)

        result = tool_compile_project({
            "project_dir": str(src_dir),
            # The MCP orchestration gate resolves the orchestration root from repo_root
            # (defaulting to project_dir); pass our repo_root so it finds the capability.
            "repo_root": str(self.repo_root),
            "language": language,
            "build_system": build_system,
            # OBJDIR/BINDIR out-of-source overrides + BIN imposed to the canonical
            # <spec_id>_runner (command-line override wins over any Makefile BIN
            # assignment). Validate.execute imposes the same BIN via the make_test env;
            # see phase_03_build.md.
            "extra_args": [f"OBJDIR={obj_dir}", f"BINDIR={bin_dir}", f"BIN={exe}"],
            "capture_limit": _FULL_CAPTURE_LIMIT,
            "orchestration_id": self.orchestration_id,
            "agent_run_id": child_arid,
            "capability_token": cap_token,
        })
        ok = bool(result.get("ok"))
        # return_code is None on a subprocess timeout; treat that as a build failure.
        rc = result.get("return_code") or 1
        stdout = result.get("stdout", "") or ""
        stderr = result.get("stderr", "") or ""
        # A compile that reports success but did NOT produce the binary at the imposed
        # bin/<spec_id>_runner (Build passes BIN=<spec_id>_runner) means the Makefile's
        # build rule does not honor $(BIN). Treat as a build failure that regenerates the
        # Makefile rather than writing a pass binary_meta pointing at a missing file (which
        # desyncs from determine_substep_status -> inconsistent escalate/fail_closed).
        binary_missing = ok and not (bin_dir / exe).is_file()
        if binary_missing:
            ok = False
        # `command_log_ref` from the handler is cwd-relative (`_path_to_ref` uses
        # Path.cwd()), which is unreliable for the in-process caller — derive it from
        # our repo_root + the known canonical placement instead. Make's in-source build
        # writes the log to <src>/command_log.jsonl (project_dir = src_dir).
        command_log_ref = self._rel(src_dir / "command_log.jsonl")

        # Full (untrimmed) per-step compiler logs in the binary dir (build has no
        # canonical stdout/stderr.log otherwise — only the lean command_log audit).
        bdir = self.repo_root / refs.binary_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "compile.stdout.log").write_text(stdout, encoding="utf-8")
        (bdir / "compile.stderr.log").write_text(stderr, encoding="utf-8")

        dep = (ir.get("dependency") or {}) if isinstance(ir, dict) else {}
        direct_deps = dep.get("direct_deps") or []
        dep_keys = [d.get("node_key") if isinstance(d, dict) else d for d in direct_deps]
        # The dependency-encapsulation contract (phase_03 §23-25,53) is enforced by the
        # post_build gate below (`validate_pipeline_semantics --stage post_build` →
        # validate_post_build_violation); binary_meta.dependency_check is metadata.
        # `resolved` is "match" only when the build itself succeeded.

        binary_meta: dict[str, Any] = {
            "binary_id": refs.binary_id,
            "node_key": refs.node_key,
            "pipeline_id": refs.pipeline_id,
            "attempt_count": 1,
            "verification_status": "pass" if ok else "fail",
            "last_fail_reason": "" if ok else "compile",
            "status": "pass" if ok else "fail",
            "validation_stage": "post_build",
            "source_source_id": refs.source_id,
            "build_system": build_system,
            "compiler": result.get("compiler") or "",
            "binary_artifact_ref": f"binary/{refs.binary_id}/bin/{exe}",
            "command_id": result.get("command_id"),
            "command_log_ref": command_log_ref,
            "command_log_path": command_log_ref,
            "build_log_ref": command_log_ref,
            "dependency_check": {"direct_deps": dep_keys,
                                 "resolved": "match" if ok else "unresolved"},
            "failure_category": None,
            "failure_source_refs": [],
            "failure_excerpt": None,
        }
        if binary_missing:
            # Makefile build-rule defect -> restart (regenerate the Makefile).
            binary_meta["failure_category"] = "make_error"
            binary_meta["last_fail_reason"] = "binary_not_built_at_bindir"
            binary_meta["failure_excerpt"] = (
                f"compile reported success but no binary at bin/{exe} (imposed BIN); the "
                f"Makefile build rule must produce $(BINDIR)/$(BIN)")
            binary_meta["failure_source_refs"] = [f"{self._rel(src_dir)}/Makefile"]
        elif not ok:
            binary_meta["failure_category"] = self._classify_build_failure_category(rc, stderr)
            binary_meta["failure_excerpt"] = "\n".join(stderr.splitlines()[-50:])
            # Point Generate at the offending source(s) (phase_03 retry trigger).
            binary_meta["failure_source_refs"] = self._extract_failure_source_refs(
                stderr, self._rel(src_dir))

        meta_path = self.repo_root / refs.binary_dir() / "binary_meta.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(binary_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        # A content failure (compile/link error, post_build violation) is recorded in
        # binary_meta (verification_status=fail + failure_category) and returns rc 0 so
        # run_phase routes it via classify_build_failure -> Generate (NOT transport
        # fail_closed). determine_substep_status reads binary_meta.verification_status,
        # so a gate failure on an otherwise-built binary still fails the substep.
        if ok:
            gate = subprocess.run(
                ["python3", "tools/validate_pipeline_semantics.py", "--stage", "post_build",
                 "--pipeline-root", refs.pipeline_ref, "--source-id", refs.source_id or ""],
                cwd=self.repo_root, env=self.env, text=True, capture_output=True, check=False)
            if gate.returncode != 0:
                binary_meta.update({
                    "verification_status": "fail", "status": "fail",
                    "failure_category": "validate_post_build_violation",
                    "failure_excerpt": "\n".join((gate.stdout + gate.stderr).splitlines()[-50:]),
                })
                meta_path.write_text(
                    json.dumps(binary_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                stderr += "\n[post_build gate fail]\n" + gate.stdout + gate.stderr
        return {"returncode": 0, "stdout": stdout, "stderr": stderr}

    # -- deterministic Validate.execute (run + quality_check + evidence promote) --

    @staticmethod
    def _required_evidence_artifacts(ir: dict[str, Any]) -> list[str]:
        """IR-declared required raw-evidence artifact types (closed set:
        metrics_basis.json / execution_trace.json / state_snapshots)."""
        io = (ir.get("io_contract") or {}) if isinstance(ir, dict) else {}
        rr = (io.get("raw_requirements") or {}) if isinstance(io, dict) else {}
        out: list[str] = []
        for e in rr.get("required_evidence") or []:
            if isinstance(e, dict) and e.get("required") and e.get("artifact"):
                out.append(str(e["artifact"]))
        return out

    def _promote_run_evidence(self, run_tmp: Path, node_dir: Path,
                              artifacts: list[str]) -> list[str]:
        """Promote the runner's `run/` output to the canonical run node dir.
        Selective per artifact type (NOT a blind copytree): the runner's auxiliary
        per-case files (e.g. execution_trace_<case>.json) are deterministically dropped.
        Returns the repo-relative raw_artifact_refs of what was promoted."""
        node_dir.mkdir(parents=True, exist_ok=True)
        for name in ("diagnostics.json", "perf.json"):
            src = run_tmp / name
            if src.exists():
                shutil.copy2(src, node_dir / name)
        raw_dst = node_dir / "raw"
        raw_dst.mkdir(parents=True, exist_ok=True)
        raw_refs: list[str] = []
        node_ref = self._rel(node_dir)
        mb = run_tmp / "raw" / "metrics_basis.json"
        if mb.exists():
            shutil.copy2(mb, raw_dst / "metrics_basis.json")
            raw_refs.append(f"{node_ref}/raw/metrics_basis.json")
        for art in artifacts:
            if art == "state_snapshots":
                sdst = raw_dst / "state_snapshots"
                sdst.mkdir(parents=True, exist_ok=True)
                for f in sorted((run_tmp / "raw" / "state_snapshots").glob("*.json")):
                    shutil.copy2(f, sdst / f.name)
                    raw_refs.append(f"{node_ref}/raw/state_snapshots/{f.name}")
            elif art == "execution_trace.json":
                src = run_tmp / "raw" / "execution_trace.json"
                if src.exists():
                    shutil.copy2(src, raw_dst / "execution_trace.json")
                    raw_refs.append(f"{node_ref}/raw/execution_trace.json")
        return raw_refs

    def _author_snapshot_schema(self, ir: dict[str, Any], node_dir: Path) -> str | None:
        """Author raw/state_snapshots/snapshot_schema.json from the IR schema +
        the per-case files actually present. Deterministic (no judgment)."""
        io = (ir.get("io_contract") or {}) if isinstance(ir, dict) else {}
        rr = (io.get("raw_requirements") or {}) if isinstance(io, dict) else {}
        entry = next((e for e in (rr.get("required_evidence") or [])
                      if isinstance(e, dict) and e.get("artifact") == "state_snapshots"), None)
        if entry is None:
            return None
        sdir = node_dir / "raw" / "state_snapshots"
        if not sdir.exists():
            return None
        schema = entry.get("schema") or {}
        present = {f.name for f in sdir.glob("*.json") if f.name != "snapshot_schema.json"}
        # Order samples by IR test_case_set declaration order (fallback: sorted).
        # strip() to match the stripped case_id identity read_case_ids imposes on
        # the runner argv / on-disk <case_id>.json name (else a whitespace-bearing
        # case_id misses `present` and silently drops to sorted order).
        case = (ir.get("case") or {}) if isinstance(ir, dict) else {}
        tcs = case.get("test_case_set") or [] if isinstance(case, dict) else []
        ordered = [f"{c['case_id'].strip()}.json" for c in tcs
                   if isinstance(c, dict) and isinstance(c.get("case_id"), str)
                   and c["case_id"].strip()
                   and f"{c['case_id'].strip()}.json" in present]
        samples = ordered + sorted(present - set(ordered))
        doc = {
            "variables": schema.get("variables", []),
            "time_variable": schema.get("time_variable"),
            "time_shape_expr": schema.get("time_shape_expr"),
            "min_samples": entry.get("min_samples", 1),
            "samples": samples,
        }
        (sdir / "snapshot_schema.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return f"{self._rel(node_dir)}/raw/state_snapshots/snapshot_schema.json"

    def _snapshot_deliverable_gap(self, snapshots_dir: Path, case_ids: list[str],
                                  artifacts: list[str]) -> str:
        """Diagnostic for a per-case snapshot deliverable mismatch, else "".

        Validate.execute's deliverable gate (build_launch_request) requires one
        raw/state_snapshots/<case_id>.json per case. The runner names snapshots
        freely, so a fixed/sequential name (snapshot_0001.json) or a combined file
        leaves the expected <case_id>.json absent and the deliverable gate fails
        with no recorded cause. This returns an actionable message (expected vs
        written vs missing) so the failure routes to Generate with a clear reason
        instead of an opaque deliverable-missing fail. Empty when snapshots are not
        required or every expected <case_id>.json is present.

        ``snapshots_dir`` is the runner's THIS-attempt output dir (the per-run tmp
        raw/state_snapshots), so the written set is fresh by construction — a stale
        correctly-named file from a prior attempt in the canonical node dir cannot
        mask a real gap (matching the gate's mtime freshness semantics).
        """
        if "state_snapshots" not in artifacts or not case_ids:
            return ""
        present = ({f.name for f in snapshots_dir.glob("*.json")
                    if f.name != "snapshot_schema.json"}
                   if snapshots_dir.exists() else set())
        expected = {f"{cid}.json" for cid in case_ids}
        missing = sorted(expected - present)
        if not missing:
            return ""
        return (
            "[execute fail: snapshot deliverable mismatch] Validate.execute requires "
            "one raw/state_snapshots/<case_id>.json per case. "
            f"expected={sorted(expected)}; runner wrote={sorted(present)}; "
            f"missing={missing}. Name each snapshot exactly <case_id>.json, built "
            "from the case_id passed via --cases (e.g. trim(case_id)//'.json'). "
            "Canonical: phase_02_generate.md / phase_04_validate.md §43."
        )

    @staticmethod
    def _author_quality_check(node_dir: Path, run_diag: dict[str, Any],
                              qc_diag: dict[str, Any], run_cmd_id: str | None,
                              qc_cmd_id: str | None, preset: str,
                              threads: int) -> str:
        """quality_check.json = deterministic value-equality of run_program vs the
        make-test re-run (per phase_04 §4-1). Returns the top-level status."""
        def _check_map(d: dict[str, Any]) -> dict[str, Any]:
            return {k: (v.get("status") if isinstance(v, dict) else v)
                    for k, v in (d.get("checks") or {}).items()}

        run_checks, qc_checks = _check_map(run_diag), _check_map(qc_diag)
        run_verdict, qc_verdict = run_diag.get("verdict"), qc_diag.get("verdict")
        verdict_available = bool(run_verdict) and bool(qc_verdict)
        diagnostics_match = run_checks == qc_checks
        verdict_match = run_verdict == qc_verdict
        run_cases = {c.get("case_id"): c.get("verdict")
                     for c in run_diag.get("cases") or [] if isinstance(c, dict)}
        qc_cases = {c.get("case_id"): c.get("verdict")
                    for c in qc_diag.get("cases") or [] if isinstance(c, dict)}
        per_case = {cid: (run_cases.get(cid) == qc_cases.get(cid)) for cid in run_cases}
        checks_match = {k: (run_checks.get(k) == qc_checks.get(k)) for k in run_checks}
        status = "pass" if (verdict_available and diagnostics_match and verdict_match) else "fail"
        doc = {
            "status": status,
            "preset": preset,
            "checks": {
                "verdict_available": verdict_available,
                "diagnostics_match": diagnostics_match,
                "verdict_match": verdict_match,
            },
            "comparison": {
                "reference": {"source": "run_program", "command_id": run_cmd_id,
                              "threads_per_rank": threads, "verdict": run_verdict},
                "candidate": {"source": f"run_quality_checks/{preset}", "command_id": qc_cmd_id,
                              "threads_per_rank": "make_default", "verdict": qc_verdict},
                "diagnostics_checks_match": checks_match,
                "per_case_verdict_match": per_case,
            },
            "notes": ("conductor in-process: run_program (threads_per_rank=1) and "
                      f"{preset} re-run diagnostics checks and verdicts compared."),
        }
        (node_dir / "quality_check.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return status

    def _rel(self, path: Path) -> str:
        """repo-root-relative POSIX path for canonical refs."""
        return str(path.relative_to(self.repo_root)).replace("\\", "/")

    def _execute_inproc(self, refs: NodeRefs, child_arid: str, cap_token: str) -> dict[str, Any]:
        """Deterministic Validate.execute: in-process run_program + run_quality_checks,
        promote the runner's primary evidence to the canonical run node dir, author the
        agent-owned metadata (snapshot_schema/quality_check/trial_meta/stdout/stderr),
        then run the post_execute gate. The runner's evidence bytes are never authored
        by an LLM (preserving Validate.judge's non-fabrication independence)."""
        import sys as _sys
        mcp_dir = str(self.repo_root / "mcp_servers")
        if mcp_dir not in _sys.path:
            _sys.path.insert(0, mcp_dir)
        from build_runtime_server import tool_run_program, tool_run_quality_checks

        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        impl = (ir.get("impl_defaults") or {}) if isinstance(ir, dict) else {}
        toolchain = (impl.get("toolchain") or {}) if isinstance(impl, dict) else {}
        target = (impl.get("target") or {}) if isinstance(impl, dict) else {}
        target_class = str(target.get("class") or "cpu")
        threads = 1
        self._require_make_build_system(
            str(toolchain.get("build_system") or "make"), "validate.execute")

        node_dir = self.repo_root / refs.run_node_dir()
        src_dir = self.repo_root / refs.source_dir() / "src"
        bin_dir = self.repo_root / refs.binary_dir(refs.source_binary_id) / "bin"
        exe = self._resolve_exe_name(refs)
        binary = (bin_dir / exe).resolve()
        ir_spec = (self.repo_root / refs.ir_ref / "spec.ir.yaml").resolve()
        run_tmp = self.repo_root / "workspace" / "tmp" / child_arid / "run"
        qc_tmp = self.repo_root / "workspace" / "tmp" / child_arid / "qc_run"
        obj_tmp = self.repo_root / "workspace" / "tmp" / child_arid / "build"
        cmd_log = node_dir / "command_log.jsonl"
        qc_cmd_log = src_dir / "command_log.jsonl"
        case_ids = list(self.read_case_ids(refs))

        # The runner opens raw/ paths relatively (cwd=RUNDIR); pre-create them.
        (run_tmp / "raw" / "state_snapshots").mkdir(parents=True, exist_ok=True)
        qc_tmp.mkdir(parents=True, exist_ok=True)

        gate_args = {"orchestration_id": self.orchestration_id,
                     "agent_run_id": child_arid, "capability_token": cap_token,
                     # so the MCP orchestration gate resolves the right orchestration root
                     "repo_root": str(self.repo_root)}

        # 1. run_program (primary evidence) — include spec.ir.yaml.case per phase_04 §4-1.
        res_run = tool_run_program({
            "project_dir": str(run_tmp),
            "command": [str(binary), "--cases", str(ir_spec), *case_ids],
            "target": {"class": target_class},
            "threads_per_rank": threads,
            "command_log_path": str(cmd_log),
            "capture_limit": _FULL_CAPTURE_LIMIT,
            **gate_args,
        })
        stdout = res_run.get("stdout", "") or ""
        stderr = res_run.get("stderr", "") or ""
        if not res_run.get("ok"):
            # Runtime error is a CONTENT failure (buggy generated code): rc 0 so run_phase
            # routes it via the validate tables / diagnostician, not transport fail_closed.
            # No trial_meta is written, so determine_substep_status fails this substep.
            return {"returncode": 0, "stdout": stdout,
                    "stderr": stderr + "\n[run_program failed: runtime_error]"}

        # 2. run_quality_checks (make_test re-run; output to a SEPARATE tmp).
        res_qc = tool_run_quality_checks({
            "project_dir": str(src_dir),
            "preset": "make_test",
            # BIN imposed to the canonical <spec_id>_runner so `make test`'s
            # `$(BINDIR)/$(BIN)` guard resolves the same binary Build produced. make_test
            # passes overrides via the environment only, which overrides the Makefile's
            # `BIN ?=` form (enforced by post_generate).
            # No dependency-source staging here (unlike _build_inproc): `make test` only runs
            # the already-built binary (the `test:` target has no build prerequisite, so it
            # never recompiles), so the closure `.f90`/`.mod` are not needed in OBJDIR.
            "env": {"OBJDIR": str(obj_tmp), "BINDIR": str(bin_dir),
                    "RUNDIR": str(qc_tmp), "BIN": str(exe)},
            "command_log_path": str(qc_cmd_log),
            "capture_limit": _FULL_CAPTURE_LIMIT,
            **gate_args,
        })

        # 3. promote primary evidence (selective per artifact type) + author metadata.
        artifacts = self._required_evidence_artifacts(ir)
        raw_refs = self._promote_run_evidence(run_tmp, node_dir, artifacts)
        schema_ref = self._author_snapshot_schema(ir, node_dir)
        if schema_ref:
            raw_refs.append(schema_ref)
        # Per-case snapshot deliverable check (build_launch_request requires one
        # raw/state_snapshots/<case_id>.json per case). Compute a clear diagnostic
        # here so a misnamed/combined snapshot fails with an actionable cause rather
        # than the opaque determine_substep_status deliverable-presence fail. Read
        # the runner's THIS-attempt tmp output (fresh), not the promoted node dir.
        snapshot_gap = self._snapshot_deliverable_gap(
            run_tmp / "raw" / "state_snapshots", case_ids, artifacts)

        run_diag = _read_json(run_tmp / "diagnostics.json") or {}
        qc_diag = _read_json(qc_tmp / "diagnostics.json") or {}
        qc_status = self._author_quality_check(
            node_dir, run_diag, qc_diag, res_run.get("command_id"),
            res_qc.get("command_id"), "make_test", threads)

        (node_dir / "stdout.log").write_text(stdout, encoding="utf-8")
        (node_dir / "stderr.log").write_text(stderr, encoding="utf-8")

        trial_meta = {
            "run_id": refs.run_id,
            "node_key": refs.node_key,
            "pipeline_id": refs.pipeline_id,
            "source_source_id": refs.source_id,
            "source_binary_id": refs.source_binary_id,
            "runner_command": shlex.join([exe, "--cases", self._rel(ir_spec), *case_ids]),
            "process_trace_ref": self._rel(cmd_log),
            "source_command_ref": {
                "run_program": {"tool_name": "run_program",
                                "command_id": res_run.get("command_id"),
                                "command_log_ref": self._rel(cmd_log)},
                "run_quality_checks": {"tool_name": "run_quality_checks",
                                       "command_id": res_qc.get("command_id"),
                                       "command_log_ref": self._rel(qc_cmd_log)},
            },
            "raw_artifact_refs": raw_refs,
            "environment": {
                "target_class": target_class,
                "backend": str(toolchain.get("backend") or "openmp"),
                "threads_per_rank": threads,
                "openmp_env": {"OMP_NUM_THREADS": str(threads), "OMP_THREAD_LIMIT": str(threads)},
            },
            "status": "pass" if qc_status == "pass" else "fail",
        }
        (node_dir / "trial_meta.json").write_text(
            json.dumps(trial_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        # 4. gates: artifact syntax + post_execute structural check.
        syn = subprocess.run(
            ["python3", "tools/check_artifact_syntax.py", "--format", "json",
             "--expect-top", "object",
             str(node_dir / "diagnostics.json"), str(node_dir / "perf.json"),
             str(node_dir / "quality_check.json")],
            cwd=self.repo_root, env=self.env, text=True, capture_output=True, check=False)
        gate = subprocess.run(
            ["python3", "tools/validate_pipeline_semantics.py", "--stage", "post_execute",
             "--pipeline-root", refs.pipeline_ref, "--run-id", refs.run_id or ""],
            cwd=self.repo_root, env=self.env, text=True, capture_output=True, check=False)
        if syn.returncode != 0 or gate.returncode != 0 or qc_status != "pass" or snapshot_gap:
            # Content failure: record it in trial_meta.status (read by
            # determine_substep_status) and return rc 0 so run_phase routes it via the
            # validate tables / diagnostician, NOT transport fail_closed.
            stderr += ("\n[execute fail]\n" + syn.stdout + syn.stderr
                       + gate.stdout + gate.stderr)
            if snapshot_gap:
                stderr += "\n" + snapshot_gap
            trial_meta["status"] = "fail"
            (node_dir / "trial_meta.json").write_text(
                json.dumps(trial_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"returncode": 0, "stdout": stdout, "stderr": stderr}

    def run_substep(self, refs: NodeRefs, phase: str, substep: str | None,
                    repair: dict[str, str] | None = None) -> SubstepOutcome:
        # Certify the codex hooks feature BEFORE record_launch: this can fail closed
        # (SandboxEnforcementError) when the feature is uncertified, and doing it here —
        # ahead of allocating an arid / recording a durable launch — avoids orphaning a
        # recorded launch (phantom `child_running` active run) on that fail-closed path.
        # Memoized per orchestration (no-op after the first); spawn_leaf also calls it as a
        # safety net for the record-launch-less diagnostician leaf.
        self._ensure_codex_feature_cache()
        child_arid = self.new_agent_run_id()
        request = build_launch_request(
            refs, step=phase, substep=substep,
            orchestration_id=self.orchestration_id,
            orchestration_agent_run_id=self.orchestration_agent_run_id,
            child_agent_run_id=child_arid,
            agent_model=self.agent_model, workflow_mode=self.workflow_mode,
            case_ids=self.read_case_ids(refs) if phase == "validate" else (),
            evidence_artifacts=self._read_evidence_artifacts(refs) if phase == "validate"
            else ("state_snapshots",),
            # build's allowed_output_paths binary path = the imposed canonical exe name.
            exe_name=(self._resolve_exe_name(refs) if phase == "build" else None),
            # leaf generate: src/Makefile is conductor-authored, so drop it from the leaf's
            # allowed_output_paths (it must not author it).
            makefile_host_authored=(phase == "generate" and self._conductor_authors_makefile(refs)),
            repair=repair,
        )
        rec = self.record_launch(child_arid, request)
        # Capture the launch instant so a producer substep only passes on outputs
        # (re)written during this child window, not stale files from a prior attempt.
        launched_at = time.time()
        if self._is_deterministic_substep(phase, substep):
            # Non-LLM step: run the body in-process and play the child-return ourselves
            # (no `claude -p` leaf). record_launch above + record-child-return here +
            # finalize_child below keep the executor a normal step/substep agent_run_id,
            # so the integrity validators pass unchanged.
            proc = self._run_deterministic_substep(refs, phase, substep, child_arid, request)
            self._persist_leaf_output(child_arid, proc, prefix="deterministic")
            token = self.read_parent_return_token(child_arid)
            self.runtime([
                "record-child-return", *self._oid_args(),
                "--agent-run-id", child_arid, "--return-token", token,
            ])
        else:
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
        # The conductor authors src/Makefile deterministically (runtime-owned, like
        # lineage.json) for every make+fortran node BEFORE the substeps run: the generate leaf
        # must not author it, and generate.verify's post_generate gate inspects it. The
        # template encodes the fixed runner->model use-graph and, for a dependency node, the
        # closure object rules (Model B); the dep sources are staged at build (see
        # _stage_dependency_sources). c/cpp/mixed keep LLM authoring (see _write_makefile).
        if phase == "generate" and self._conductor_authors_makefile(refs):
            self._write_makefile(refs)

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
            "required_outputs": phase_required_outputs(
                refs, phase,
                exe_name=(self._resolve_exe_name(refs) if phase == "build" else None),
                makefile_required=not (phase == "generate" and self._conductor_authors_makefile(refs))),
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
        # A nonzero leaf exit is an infra/transport failure (token limit, OOM, transport,
        # session limit) the decision tables cannot classify — route straight to fail_closed
        # so the operator can --resume. This is checked BEFORE write_step_result: a transport
        # failure leaves no canonical evidence (e.g. a judge that died with no
        # semantic_review.json), so writing the step_result would crash on the
        # post_phase_complete judge gate instead of cleanly failing closed. Skipping the write
        # leaves the attempt's already-terminalized agents without a step_result, so tombstone
        # them (add-superseded-runs) — otherwise a later --resume (which re-runs the phase fresh)
        # trips _validate_orchestration_completion_for_pass on the orphaned arids. Tombstone
        # EVERY outcome arid: substep agents for substep-aware phases, or the single step-role
        # agent for build (substep_arids is empty there, but outcomes[0] is recorded in
        # agent_runs.jsonl and would be flagged as a step orphan).
        transport = (next((oc for oc in outcomes if oc.leaf_returncode != 0), None)
                     if status != "pass" else None)
        if transport is not None:
            orphan_arids = [oc.agent_run_id for oc in outcomes]
            if orphan_arids:
                self._add_superseded_run_ids(
                    orphan_arids,
                    reason=f"leaf_transport_error_orphan: leaf_exit={transport.leaf_returncode}")
            decision = RouteDecision(
                "fail_closed",
                reason=f"leaf_transport_error: leaf_exit={transport.leaf_returncode}")
            return PhaseOutcome(phase, status, substep_arids, failed, decision)

        self.write_step_result(node_key, phase, executor, result)
        if status == "pass":
            decision = RouteDecision("advance")
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
            # An execute-substep failure (deterministic) means judge never ran, so there
            # is no verdict.json. The runner produced bad/missing primary evidence (a
            # runtime error or a post_execute structural violation), which is a code
            # defect -> regenerate. Route to Generate deterministically rather than
            # escalating with no verdict (the judge-centric table can't classify it).
            if not (self.repo_root / refs.run_node_dir() / "verdict.json").is_file():
                # Backstop (C2): a Generate restart regenerates the RUNNER, which cannot
                # fix an IR-rooted structural mismatch (the runner keeps emitting its
                # natural shape; the IR is the wrong side). Count execute (no-verdict)
                # failures per node; once a Generate restart has already failed to fix one
                # (threshold 2, still within MAX_ATTEMPTS_PER_PHASE=3), attribute the
                # defect to the IR and reopen Compile instead of looping Generate.
                #
                # The counter resets BOTH (a) when escalating to Compile here and (b) when
                # validate advances (conduct, on validate pass). (a) is essential: the
                # Compile reopen regenerates the IR (and downstream source), so the next
                # execute failure is against FRESH artifacts and must get its own
                # Generate-retry-first cycle rather than immediately re-escalating because
                # a stale count is still >= 2.
                if not hasattr(self, "_validate_execute_fail_count"):
                    self._validate_execute_fail_count: dict[str, int] = {}
                count = self._validate_execute_fail_count.get(refs.node_key, 0) + 1
                if count >= C2_EXECUTE_FAIL_ESCALATION_THRESHOLD:
                    self._validate_execute_fail_count[refs.node_key] = 0
                    return RouteDecision("reopen", target_phase="compile",
                                         reason="validate_execute_fail_ir")
                self._validate_execute_fail_count[refs.node_key] = count
                return RouteDecision("retry", target_phase="generate", repair_strategy="restart",
                                     reason="validate_execute_fail")
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
                # validate advanced: a later, unrelated execute failure should start its
                # escalation count fresh (C2 backstop counter).
                if phase == "validate" and hasattr(self, "_validate_execute_fail_count"):
                    self._validate_execute_fail_count.pop(refs.node_key, None)
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

            target_idx = phases.index(target)
            # F1: dev confines auto-retry to WITHIN a single phase (the run_phase substep
            # loop, e.g. generate.generate -> generate.verify -> regenerate). A cross-phase
            # backward rollback — the only routing that actually reopens an already-passed
            # upstream phase (target_idx < idx, the branch below) — fail_closes immediately so
            # the operator sees the structural issue on the first occurrence instead of burning
            # the whole attempt budget on a regeneration loop that cannot fix it (the
            # C1/C2/D2 "regenerating one side can't fix the other" pattern). This generalizes
            # the older dev verify/judge-severity gate (classify_verify_severity) to "any
            # cross-phase rollback regardless of failure classification". `target_idx < idx`
            # already covers every real reopen (they all target compile = upstream) and every
            # earlier-phase retry; a same-phase/forward (malformed) reopen is NOT a backward
            # rollback, so it falls through to the `target_idx >= idx` terminal-fail branch
            # below — same as prod — rather than being mislabeled a dev_phase_rollback. prod
            # keeps today's bounded cross-phase reopen/retry (the C2 backstop's compile reopen
            # stays live for prod and is a no-op here for dev).
            if self.workflow_mode == "dev" and target_idx < idx:
                self.set_status("fail_closed", reason_code="dev_phase_rollback",
                                reason_detail=(decision.reason or f"{phase}->{target}")[:200])
                return "fail_closed"

            attempts[target] += 1
            if attempts[target] > MAX_ATTEMPTS_PER_PHASE:
                self.set_status("fail_closed", reason_code="retry_budget_exhausted",
                                reason_detail=f"{target} exceeded {MAX_ATTEMPTS_PER_PHASE}")
                return "fail_closed"

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


def phase_required_outputs(refs: NodeRefs, phase: str, exe_name: str | None = None,
                           *, makefile_required: bool = True) -> list[str]:
    if phase == "compile":
        return [f"{refs.ir_ref}/spec.ir.yaml", f"{refs.ir_ref}/ir_meta.json"]
    if phase == "generate":
        src = refs.source_dir()
        # lineage.json is authored host-side by the conductor (_write_lineage), not a leaf
        # output_ref, so it is NOT a step required_output (which must be covered by the
        # producer leaf's output_refs). post_generate still verifies it independently. For a
        # leaf node src/Makefile is likewise conductor-authored (_write_makefile), so it is
        # excluded when makefile_required is False (same rationale as lineage).
        make_entry = [f"{src}/src/Makefile"] if makefile_required else []
        return [
            f"{src}/src/{refs.spec_id}_model.f90",
            f"{src}/src/{refs.spec_id}_runner.f90",
            *make_entry,
            f"{src}/source_meta.json",
        ]
    if phase == "build":
        bdir = refs.binary_dir()
        # The binary basename = the imposed canonical exe name (mirrors
        # build_launch_request's exe_name); the <spec_id>_runner fallback applies only
        # when no exe_name is threaded (non-build callers).
        return [
            f"{bdir}/bin/{exe_name or (refs.spec_id + '_runner')}",
            f"{bdir}/binary_meta.json",
            f"{refs.source_dir()}/src/command_log.jsonl",
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
    # An explicit --agent-model wins; otherwise fall back to the backend's unpinned
    # spec-side alias (claude -> settings alias / "opus"; codex -> "codex"). Never a
    # pinned version: the exact version is resolved post-run from the leaf transcript.
    from tools.orchestration_runtime import default_agent_model_for_backend
    resolved_agent_model = agent_model or default_agent_model_for_backend(backend)
    conductor = Conductor(
        repo_root=root, orchestration_id=orchestration_id,
        orchestration_agent_run_id=orchestration_agent_run_id,
        backend=backend, env=env,
        agent_model=resolved_agent_model, workflow_mode=workflow_mode,
        llm_command=llm_command,
    )
    refs = (resume_node_refs(conductor, node_key, spec_path) if resume
            else prepare_node(conductor, node_key, spec_path))
    return conductor.conduct(refs, until_phase.lower())

