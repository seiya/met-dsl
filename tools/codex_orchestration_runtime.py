#!/usr/bin/env python3
"""Helpers for Codex workflow orchestration artifacts."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


TERMINAL_STATUSES = {"pass", "fail", "blocked", "timeout", "cancel"}
SUPPORTED_BACKENDS = {"codex", "cursor", "claude"}

# Must match tools/validate_workspace_root.py (canonical pipeline/plan id directory naming).
_NODE_KEY_SAFE_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*__[a-z0-9][a-z0-9_]*__[0-9][0-9A-Za-z._-]*$"
)
DEFAULT_BACKEND_COMMANDS = {
    "codex": "codex",
    "cursor": "agent",
    "claude": "claude",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text if text.endswith("\n") else f"{text}\n"
    path.write_text(body, encoding="utf-8")


def _orchestration_root(repo_root: Path, orchestration_id: str) -> Path:
    return repo_root / "workspace" / "orchestrations" / orchestration_id


def _preflight_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "preflight.json"


def _preflight_allows_agent_launch(payload: dict[str, Any]) -> bool:
    feature_states = payload.get("feature_states")
    if not isinstance(feature_states, dict):
        return False
    if feature_states.get("multi_agent") is not True:
        return False

    checks = payload.get("checks")
    if not isinstance(checks, list):
        return False
    multi_agent_check_pass: bool | None = None
    for item in checks:
        if not isinstance(item, dict):
            continue
        if item.get("name") != "multi_agent_enabled":
            continue
        pass_value = item.get("pass")
        if isinstance(pass_value, bool):
            multi_agent_check_pass = pass_value
            break

    return (
        payload.get("status") == "pass"
        and payload.get("can_launch_step_agents") is True
        and payload.get("can_launch_substep_agents") is True
        and multi_agent_check_pass is True
    )


def _validate_preflight_payload(payload: dict[str, Any]) -> None:
    if (
        payload.get("can_launch_step_agents") is True
        or payload.get("can_launch_substep_agents") is True
    ) and payload.get("status") != "pass":
        raise ValueError(
            "preflight status must be pass when can_launch_step_agents/can_launch_substep_agents is true"
        )

    feature_states = payload.get("feature_states")
    if isinstance(feature_states, dict):
        multi_agent = feature_states.get("multi_agent")
        if isinstance(multi_agent, bool) and not multi_agent:
            if payload.get("can_launch_step_agents") is True or payload.get(
                "can_launch_substep_agents"
            ) is True:
                raise ValueError(
                    "feature_states.multi_agent=false is incompatible with launchable preflight"
                )

    checks = payload.get("checks")
    if isinstance(checks, list):
        multi_agent_check_pass: bool | None = None
        for item in checks:
            if not isinstance(item, dict):
                continue
            if item.get("name") != "multi_agent_enabled":
                continue
            pass_value = item.get("pass")
            if isinstance(pass_value, bool):
                multi_agent_check_pass = pass_value
                break
        if multi_agent_check_pass is False:
            if payload.get("can_launch_step_agents") is True or payload.get(
                "can_launch_substep_agents"
            ) is True:
                raise ValueError(
                    "checks.multi_agent_enabled.pass=false is incompatible with launchable preflight"
                )
    elif (
        payload.get("status") == "pass"
        and payload.get("can_launch_step_agents") is True
        and payload.get("can_launch_substep_agents") is True
    ):
        raise ValueError(
            "checks must include multi_agent_enabled.pass=true when preflight is launchable"
        )

    if (
        payload.get("status") == "pass"
        and payload.get("can_launch_step_agents") is True
        and payload.get("can_launch_substep_agents") is True
    ):
        if not isinstance(feature_states, dict) or feature_states.get("multi_agent") is not True:
            raise ValueError(
                "feature_states.multi_agent=true is required when preflight is launchable"
            )


def _require_preflight_launchable(
    repo_root: Path,
    orchestration_id: str,
    *,
    enforce_live_probe: bool = True,
) -> dict[str, Any]:
    path = _preflight_path(repo_root, orchestration_id)
    if not path.exists():
        raise RuntimeError(f"preflight missing: {path}")
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise RuntimeError(f"preflight must be object: {path}")
    if not _preflight_allows_agent_launch(payload):
        raise RuntimeError(
            "preflight gate failed: launchable preflight with multi_agent=true is required"
        )
    if enforce_live_probe and _live_preflight_enforced():
        backend = payload.get("backend")
        if not isinstance(backend, str) or backend.strip() not in SUPPORTED_BACKENDS:
            backend = "codex"
        command = payload.get("probe_command")
        probe_command = command.strip() if isinstance(command, str) and command.strip() else None
        live_probe = probe_execution_platform(backend=backend, agent_command=probe_command)
        if not _preflight_allows_agent_launch(live_probe):
            raise RuntimeError(
                "live preflight gate failed: execution platform multi_agent must be enabled at launch time"
            )
    return payload


def _live_preflight_enforced() -> bool:
    raw = os.environ.get("CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT", "1").strip().lower()
    return raw not in {"0", "false", "no"}


def _launch_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/launches/{agent_run_id}"
    return f"{prefix}.request.json", f"{prefix}.response.json"


def _launch_dialog_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/launches/{agent_run_id}"
    return f"{prefix}.prompt.txt", f"{prefix}.reply.txt"


def _child_launch_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/agents/{agent_run_id}/dialogs/child"
    return f"{prefix}.request.json", f"{prefix}.response.json"


def _child_dialog_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/agents/{agent_run_id}/dialogs/child"
    return f"{prefix}.prompt.txt", f"{prefix}.reply.txt"


def _agent_result_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/agents/{agent_run_id}/dialogs/agent"
    return f"{prefix}.result.json", f"{prefix}.summary.txt"


def _coerce_launch_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    if isinstance(value, (bool, int, float)):
        return str(value)
    return None


def _coerce_nested_launch_text(payload: dict[str, Any], path: tuple[str, ...]) -> str | None:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _coerce_launch_text(current)


def _is_placeholder_ref(value: str) -> bool:
    token = value.strip()
    if not token:
        return False
    return "agent-determined" in token or ("<" in token and ">" in token)


def _launch_prompt_template_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "skills"
        / "workflow-orchestration"
        / "references"
        / "launch_prompts.md"
    )


@lru_cache(maxsize=1)
def _load_launch_prompt_templates() -> dict[str, str]:
    text = _launch_prompt_template_path().read_text(encoding="utf-8")
    pattern = re.compile(
        r"## `(?P<name>step agent|substep agent)` 起動要求テンプレート\s+```text\n(?P<body>.*?)\n```",
        re.DOTALL,
    )
    templates: dict[str, str] = {}
    for match in pattern.finditer(text):
        templates[match.group("name")] = match.group("body")
    if set(templates) != {"step agent", "substep agent"}:
        raise RuntimeError("launch prompt templates must define step agent and substep agent")
    return templates


def _launch_prompt_template_name(request_payload: dict[str, Any]) -> str:
    substep = request_payload.get("substep")
    if isinstance(substep, str) and substep.strip():
        return "substep agent"
    return "step agent"


def _template_placeholder_values(request_payload: dict[str, Any]) -> dict[str, str]:
    return {
        "node_key": str(request_payload.get("node_key", "")),
        "step": str(request_payload.get("step", "")),
        "substep": str(request_payload.get("substep", "")),
        "orchestration_id": str(request_payload.get("orchestration_id", "")),
        "agent_run_id": str(request_payload.get("agent_run_id", "")),
        "parent_agent_run_id": str(request_payload.get("parent_agent_run_id", "")),
        "plan_ref": str(request_payload.get("plan_ref", "")),
        "pipeline_ref": str(request_payload.get("pipeline_ref", "")),
        "dependency_ref": str(request_payload.get("dependency_ref", "")),
        "skill_name": str(request_payload.get("skill_name", "")),
        "skill_ref": str(request_payload.get("skill_ref", "")),
        "skill_must_read_refs": str(request_payload.get("skill_must_read_refs", "")),
        "issue_severity": str(request_payload.get("issue_severity", "")),
        "repair_strategy": str(request_payload.get("repair_strategy", "")),
        "repair_target_agent_run_id": str(request_payload.get("repair_target_agent_run_id", "")),
        "repair_reason": str(request_payload.get("repair_reason", "")),
    }


def _render_launch_prompt_template(request_payload: dict[str, Any]) -> str:
    template = _load_launch_prompt_templates()[_launch_prompt_template_name(request_payload)]
    rendered = template
    for key, value in _template_placeholder_values(request_payload).items():
        rendered = rendered.replace(f"<{key}>", value)
    return rendered


def build_launch_prompt_text(request_payload: dict[str, Any]) -> str:
    return _render_launch_prompt_template(request_payload).split("\n\n", 1)[0]


def _skill_name_for_request(request_payload: dict[str, Any]) -> str | None:
    step = request_payload.get("step")
    if not isinstance(step, str) or not step.strip():
        return None
    step_token = step.strip().lower()
    substep = request_payload.get("substep")
    if isinstance(substep, str) and substep.strip():
        return f"workflow-{step_token}-{substep.strip().lower()}"
    return f"workflow-{step_token}"


def _required_verify_skill_refs(request_payload: dict[str, Any]) -> list[str]:
    step = request_payload.get("step")
    substep = request_payload.get("substep")
    plan_ref = request_payload.get("plan_ref")
    if (
        not isinstance(step, str)
        or step.strip().lower() not in {"plan", "generate"}
        or not isinstance(substep, str)
        or substep.strip().lower() != "verify"
        or not isinstance(plan_ref, str)
        or not plan_ref.strip()
    ):
        return []
    plan_root = plan_ref.strip().rstrip("/")
    refs = [
        f"{plan_root}/case.resolved.yaml",
        f"{plan_root}/algorithm.resolved.yaml",
        f"{plan_root}/impl.resolved.yaml",
        f"{plan_root}/dependency.resolved.yaml",
        f"{plan_root}/derived_contract.json",
    ]
    if step.strip().lower() == "generate":
        pipeline_ref = request_payload.get("pipeline_ref")
        generation_id = request_payload.get("generation_id")
        if not isinstance(pipeline_ref, str) or not pipeline_ref.strip():
            raise ValueError("generate verify launch request must include non-empty pipeline_ref")
        if not isinstance(generation_id, str) or not generation_id.strip():
            raise ValueError("generate verify launch request must include non-empty generation_id")
        pr = pipeline_ref.strip().rstrip("/")
        gid = generation_id.strip()
        refs.extend(
            [
                f"{pr}/lineage.json",
                f"{pr}/generate/{gid}/generate_meta.json",
            ]
        )
    return refs


def _merge_unique_refs(*ref_groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in ref_groups:
        for ref in group:
            token = ref.strip()
            if not token or token in seen:
                continue
            merged.append(token)
            seen.add(token)
    return merged


def build_skill_must_read_refs(request_payload: dict[str, Any]) -> list[str]:
    skill_ref = request_payload.get("skill_ref")
    skill_refs = [skill_ref.strip()] if isinstance(skill_ref, str) and skill_ref.strip() else []
    existing_refs = _split_skill_refs(request_payload.get("skill_must_read_refs"))
    common_refs = ["docs/WORKFLOW.md", "docs/ORCHESTRATION.md"]
    verify_refs = _required_verify_skill_refs(request_payload)
    return _merge_unique_refs(skill_refs, common_refs, existing_refs, verify_refs)


def render_launch_prompt_text(request_payload: dict[str, Any]) -> str:
    return _render_launch_prompt_template(request_payload)


def prepare_launch_request_payload(request_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(request_payload)
    if not isinstance(payload.get("skill_name"), str) or not payload.get("skill_name", "").strip():
        skill_name = _skill_name_for_request(payload)
        if skill_name is not None:
            payload["skill_name"] = skill_name
    if not isinstance(payload.get("skill_ref"), str) or not payload.get("skill_ref", "").strip():
        skill_name = payload.get("skill_name")
        if isinstance(skill_name, str) and skill_name.strip():
            payload["skill_ref"] = f"skills/{skill_name.strip()}/SKILL.md"
    payload.setdefault("issue_severity", "none")
    payload.setdefault("repair_strategy", "none")
    payload.setdefault("repair_target_agent_run_id", "none")
    payload.setdefault("repair_reason", "none")
    payload["skill_must_read_refs"] = ",".join(build_skill_must_read_refs(payload))
    explicit_prompt_present = any(
        _coerce_nested_launch_text(payload, path) is not None
        for path in (
            ("launch_prompt_full",),
            ("execution_prompt",),
            ("prompt",),
            ("task",),
            ("instruction",),
            ("instructions",),
            ("message",),
            ("spawn_request", "prompt"),
            ("spawn_request", "task"),
            ("spawn_request", "instruction"),
            ("spawn_request", "instructions"),
            ("spawn_request", "message"),
            ("launch_prompt",),
        )
    )
    if not explicit_prompt_present:
        payload["launch_prompt_full"] = render_launch_prompt_text(payload)
    return payload


def _extract_launch_prompt_text(request_payload: dict[str, Any]) -> str:
    # Prefer explicit full execution prompts, then fall back to short launch summaries.
    for path in (
        ("launch_prompt_full",),
        ("execution_prompt",),
        ("prompt",),
        ("task",),
        ("instruction",),
        ("instructions",),
        ("message",),
        ("spawn_request", "prompt"),
        ("spawn_request", "task"),
        ("spawn_request", "instruction"),
        ("spawn_request", "instructions"),
        ("spawn_request", "message"),
        ("launch_prompt",),
    ):
        text = _coerce_nested_launch_text(request_payload, path)
        if text is not None:
            return text
    return json.dumps(request_payload, ensure_ascii=False, indent=2)


def _extract_launch_reply_text(response_payload: dict[str, Any]) -> str:
    for key in ("launch_reply", "reply", "response_text", "message", "result"):
        text = _coerce_launch_text(response_payload.get(key))
        if text is not None:
            return text
    return json.dumps(response_payload, ensure_ascii=False, indent=2)


def _extract_response_agent_session_id(response_payload: dict[str, Any]) -> str | None:
    candidate_paths: tuple[tuple[str, ...], ...] = (
        ("agent_session_id",),
        ("agent_id",),
        ("session_id",),
        ("child_agent_id",),
        ("child_agent_session_id",),
        ("id",),
        ("agent", "id"),
        ("agent", "session_id"),
        ("child_agent", "id"),
        ("child_agent", "session_id"),
        ("data", "id"),
        ("data", "session_id"),
    )
    for path in candidate_paths:
        current: Any = response_payload
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, str) and current.strip():
            return current.strip()
    return None


def _validate_response_agent_session_id(response_payload: dict[str, Any]) -> str:
    session_id = _extract_response_agent_session_id(response_payload)
    if session_id is None:
        raise ValueError("launch response must include child agent identifier from spawn_agent")
    if _is_placeholder_ref(session_id):
        raise ValueError("launch response child agent identifier must not contain placeholder tokens")
    return session_id


def _required_launch_prompt_markers(request_payload: dict[str, Any]) -> list[str]:
    step = request_payload.get("step")
    if not isinstance(step, str) or not step.strip():
        return []
    markers = [
        "orchestration_id:",
        "agent_run_id:",
        "parent_agent_run_id:",
        "plan_ref:",
        "pipeline_ref:",
        "dependency_ref:",
        "skill_name:",
        "skill_ref:",
        "skill_must_read_refs:",
        "必須要件:",
    ]
    substep = request_payload.get("substep")
    if isinstance(substep, str) and substep.strip():
        return [
            "あなたは substep agent である。",
            "対象 node_key:",
            "対象 step:",
            "対象 substep:",
            *markers,
        ]
    return [
        "あなたは step agent である。",
        "対象 node_key:",
        "対象 step:",
        *markers,
    ]


def _required_launch_prompt_lines(request_payload: dict[str, Any]) -> list[str]:
    step = request_payload.get("step")
    if not isinstance(step, str) or not step.strip():
        return []
    return build_launch_prompt_text(request_payload).splitlines()


def _validate_launch_prompt_text(request_payload: dict[str, Any], prompt_text: str) -> None:
    required_markers = _required_launch_prompt_markers(request_payload)
    if not required_markers:
        return
    missing_markers = [marker for marker in required_markers if marker not in prompt_text]
    if missing_markers:
        raise ValueError(
            "launch prompt text must preserve workflow-orchestration template markers: "
            + ", ".join(missing_markers)
        )
    required_lines = _required_launch_prompt_lines(request_payload)
    missing_lines = [line for line in required_lines if line not in prompt_text]
    if missing_lines:
        raise ValueError(
            "launch prompt text must preserve workflow-orchestration template field values: "
            + ", ".join(missing_lines)
        )


def _extract_agent_summary_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in (
        "agent_run_id",
        "agent_role",
        "node_key",
        "step",
        "substep",
        "status",
        "agent_backend",
        "agent_model",
        "context_id",
        "agent_session_id",
        "started_at",
        "finished_at",
        "result_summary",
    ):
        value = payload.get(key)
        if value is None:
            continue
        lines.append(f"{key}: {value}")

    output_refs = payload.get("output_refs")
    if isinstance(output_refs, list) and output_refs:
        lines.append("output_refs:")
        for item in output_refs:
            if isinstance(item, str) and item.strip():
                lines.append(f"- {item.strip()}")

    if lines:
        return "\n".join(lines)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _validate_agent_summary_text(payload: dict[str, Any], summary_text: str) -> None:
    text = summary_text.strip()
    if not text:
        raise ValueError("agent.summary.txt must be non-empty")
    non_empty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(non_empty_lines) < 2:
        raise ValueError("agent.summary.txt must not be single-line summary")

    status = payload.get("status")
    if isinstance(status, str) and status.strip():
        marker = f"status: {status.strip()}"
        if marker not in text:
            raise ValueError("agent.summary.txt must include final status line")

    agent_role = payload.get("agent_role")
    if isinstance(agent_role, str) and agent_role.strip().lower() == "orchestration":
        return

    output_refs = payload.get("output_refs")
    if isinstance(output_refs, list) and any(isinstance(item, str) and item.strip() for item in output_refs):
        if "output_refs:" not in text:
            raise ValueError("agent.summary.txt must include output_refs section for pass result")
    elif (
        isinstance(status, str)
        and status.strip().lower() in TERMINAL_STATUSES
        and not any(token in text for token in ("result_summary:", "summary:", "reason:", "failure_reason:"))
    ):
        raise ValueError("agent.summary.txt must include summary or failure reason")


def _split_skill_refs(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        refs: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                refs.append(item.strip())
        return refs
    return []


def _node_key_to_safe(node_key: str) -> str:
    token = node_key.strip()
    if "/" not in token or "@" not in token:
        raise ValueError(f"invalid node_key: {node_key}")
    spec_kind, tail = token.split("/", 1)
    spec_id, spec_version = tail.rsplit("@", 1)
    spec_kind = spec_kind.strip()
    spec_id = spec_id.strip()
    spec_version = spec_version.strip()
    if not spec_kind or not spec_id or not spec_version:
        raise ValueError(f"invalid node_key: {node_key}")
    return f"{spec_kind}__{spec_id}__{spec_version}"


def _validate_canonical_workspace_root_ref(
    *,
    ref: str,
    node_safe: str,
    kind: str,
    label: str,
) -> None:
    """Require ref == workspace/{kind}/{node_safe}/{root_id} with no extra path segments."""
    token = ref.strip().strip("/")
    parts = token.split("/")
    if len(parts) != 4:
        raise ValueError(
            f"launch request {label} must be exactly workspace/{kind}/<node_key_safe>/<id> "
            f"(directory root only); got {ref!r}"
        )
    if parts[0] != "workspace" or parts[1] != kind:
        raise ValueError(f"launch request {label} must be under workspace/{kind}/; got {ref!r}")
    seg_node = parts[2]
    root_id = parts[3]
    if seg_node != node_safe:
        raise ValueError(
            f"launch request {label} node directory must be {node_safe!r}; got {ref!r}"
        )
    if not _NODE_KEY_SAFE_PATTERN.match(seg_node):
        raise ValueError(f"launch request {label} has invalid node_key_safe segment: {ref!r}")
    if not root_id.startswith(f"{node_safe}_"):
        raise ValueError(
            f"launch request {label} root id must start with {node_safe + '_'}; got {ref!r}"
        )


def _workspace_path_is_under_ref(path: str, base: str) -> bool:
    p = path.strip().rstrip("/")
    b = base.strip().rstrip("/")
    return p == b or p.startswith(b + "/")


def _validate_pass_output_refs_against_launch(
    repo_root: Path,
    payload: dict[str, Any],
) -> None:
    """Require each output_ref to lie under plan_ref or pipeline_ref from the saved launch request.

    Only applies to ``step`` / ``substep`` runs that have a launch request on disk.
    ``orchestration`` and other roles do not set ``launch_request_ref``; skip validation.
    """
    role = payload.get("agent_role")
    if not isinstance(role, str) or role.strip().lower() not in {"step", "substep"}:
        return

    output_refs = payload.get("output_refs")
    if not isinstance(output_refs, list) or not output_refs:
        return

    launch_request_ref = payload.get("launch_request_ref")
    if not isinstance(launch_request_ref, str) or not launch_request_ref.strip():
        raise ValueError("launch_request_ref must be non-empty string for pass output_refs validation")
    launch_path = repo_root / launch_request_ref.strip()
    if not launch_path.exists():
        raise ValueError(f"launch_request_ref target not found: {launch_request_ref}")
    try:
        launch_payload = _read_json(launch_path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"launch_request_ref must be valid json: {launch_request_ref}") from exc
    if not isinstance(launch_payload, dict):
        raise ValueError(f"launch request must be object: {launch_request_ref}")

    plan_ref = launch_payload.get("plan_ref")
    pipeline_ref = launch_payload.get("pipeline_ref")
    if not isinstance(plan_ref, str) or not plan_ref.strip():
        raise ValueError("launch request plan_ref missing for output_refs validation")
    if not isinstance(pipeline_ref, str) or not pipeline_ref.strip():
        raise ValueError("launch request pipeline_ref missing for output_refs validation")

    plan_root = plan_ref.strip().rstrip("/")
    pipe_root = pipeline_ref.strip().rstrip("/")

    for idx, ref in enumerate(output_refs):
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"output_refs[{idx}] must be non-empty string")
        r = ref.strip()
        if not r.startswith("workspace/"):
            raise ValueError(f"output_refs[{idx}] must start with workspace/: {r!r}")
        if _workspace_path_is_under_ref(r, plan_root) or _workspace_path_is_under_ref(r, pipe_root):
            continue
        raise ValueError(
            f"output_refs[{idx}] must be under plan_ref or pipeline_ref root "
            f"({plan_root!r} or {pipe_root!r}); got {r!r}"
        )


def _validate_launch_request_payload(request_payload: dict[str, Any]) -> None:
    node_key = request_payload.get("node_key")
    step = request_payload.get("step")
    substep = request_payload.get("substep")
    if isinstance(node_key, str) and node_key.strip():
        node_safe = _node_key_to_safe(node_key.strip())
    else:
        node_safe = None

    for key in ("plan_ref", "pipeline_ref", "dependency_ref"):
        value = request_payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"launch request must include non-empty {key}")
        if _is_placeholder_ref(value):
            raise ValueError(f"launch request {key} must not contain placeholder tokens")

    plan_ref = request_payload.get("plan_ref")
    pipeline_ref = request_payload.get("pipeline_ref")
    dependency_ref = request_payload.get("dependency_ref")
    if node_safe is not None:
        if isinstance(plan_ref, str) and plan_ref.strip():
            _validate_canonical_workspace_root_ref(
                ref=plan_ref,
                node_safe=node_safe,
                kind="plans",
                label="plan_ref",
            )
        if isinstance(pipeline_ref, str) and pipeline_ref.strip():
            _validate_canonical_workspace_root_ref(
                ref=pipeline_ref,
                node_safe=node_safe,
                kind="pipelines",
                label="pipeline_ref",
            )
    if isinstance(dependency_ref, str) and _is_placeholder_ref(dependency_ref):
        raise ValueError("launch request dependency_ref must not contain placeholder tokens")

    is_verify_substep = (
        isinstance(step, str)
        and step.strip().lower() in {"plan", "generate"}
        and isinstance(substep, str)
        and substep.strip().lower() == "verify"
    )
    if is_verify_substep and isinstance(step, str) and step.strip().lower() == "generate":
        gen_id = request_payload.get("generation_id")
        if not isinstance(gen_id, str) or not gen_id.strip():
            raise ValueError("generate verify launch request must include non-empty generation_id")

    if not is_verify_substep:
        return

    skill_name = request_payload.get("skill_name")
    skill_ref = request_payload.get("skill_ref")
    skill_must_read_refs = _split_skill_refs(request_payload.get("skill_must_read_refs"))

    if not isinstance(skill_name, str) or not skill_name.strip():
        raise ValueError("verify launch request must include non-empty skill_name")
    if not isinstance(skill_ref, str) or not skill_ref.strip():
        raise ValueError("verify launch request must include non-empty skill_ref")
    if not skill_must_read_refs:
        raise ValueError("verify launch request must include non-empty skill_must_read_refs")

    required_refs = _required_verify_skill_refs(request_payload)

    missing_refs = [ref for ref in required_refs if ref not in skill_must_read_refs]
    if missing_refs:
        raise ValueError(
            "request payload skill_must_read_refs missing required verify inputs: "
            + ", ".join(missing_refs)
        )


def _load_run_records(orchestration_root: Path) -> dict[str, dict[str, Any]]:
    runs_path = orchestration_root / "agent_runs.jsonl"
    records: dict[str, dict[str, Any]] = {}
    if not runs_path.exists():
        return records
    for raw in runs_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            continue
        run_id = item.get("agent_run_id")
        if isinstance(run_id, str) and run_id.strip():
            records[run_id.strip()] = item
    return records


def _validate_terminal_run_payload(
    repo_root: Path,
    orchestration_id: str,
    payload: dict[str, Any],
) -> None:
    role = payload.get("agent_role")
    status = payload.get("status")
    if not isinstance(role, str) or role not in {"step", "substep"}:
        return
    if not isinstance(status, str) or status.strip().lower() != "pass":
        return

    output_refs = payload.get("output_refs")
    if not isinstance(output_refs, list) or not output_refs:
        raise ValueError("pass status for step/substep requires non-empty output_refs")
    for idx, item in enumerate(output_refs):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"output_refs[{idx}] must be non-empty string")

    _validate_pass_output_refs_against_launch(repo_root, payload)


def _validate_step_or_substep_launch_refs(repo_root: Path, payload: dict[str, Any]) -> None:
    for key in (
        "launch_request_ref",
        "launch_response_ref",
        "launch_prompt_ref",
        "launch_reply_ref",
    ):
        ref = payload.get(key)
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"{key} must be non-empty string")
        target = repo_root / ref.strip()
        if not target.exists():
            raise ValueError(f"{key} target not found: {ref}")
        if key in {"launch_prompt_ref", "launch_reply_ref"}:
            text = target.read_text(encoding="utf-8", errors="ignore")
            if not text.strip():
                raise ValueError(f"{key} target must be non-empty: {ref}")


def _iter_step_result_paths(root: Path) -> list[Path]:
    steps_root = root / "steps"
    if not steps_root.exists():
        return []
    return sorted(steps_root.glob("*/*/*/step_result.json"))


def _validate_orchestration_completion_for_pass(
    repo_root: Path,
    orchestration_id: str,
) -> None:
    root = _orchestration_root(repo_root, orchestration_id)
    graph_path = root / "agent_graph.json"
    runs = _load_run_records(root)
    if not runs:
        raise RuntimeError("cannot mark orchestration pass without agent_runs.jsonl records")

    orchestration_runs = [
        payload
        for payload in runs.values()
        if isinstance(payload.get("agent_role"), str)
        and payload.get("agent_role") == "orchestration"
    ]
    if not orchestration_runs:
        raise RuntimeError("cannot mark orchestration pass without orchestration agent run record")

    graph = _load_graph(graph_path)
    edges = graph.get("edges")
    if not isinstance(edges, list) or not edges:
        raise RuntimeError("cannot mark orchestration pass without agent_graph edges")

    step_result_refs_by_substep: dict[str, Path] = {}
    for result_path in _iter_step_result_paths(root):
        try:
            result = _read_json(result_path)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid step_result.json: {result_path}") from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"step_result.json must be object: {result_path}")
        executor_run_id = result.get("executor_agent_run_id")
        if not isinstance(executor_run_id, str) or not executor_run_id.strip():
            raise RuntimeError(f"executor_agent_run_id missing: {result_path}")
        substep_run_ids = result.get("substep_agent_run_ids")
        if not isinstance(substep_run_ids, list):
            raise RuntimeError(f"substep_agent_run_ids must be list: {result_path}")
        for substep_run_id in substep_run_ids:
            if isinstance(substep_run_id, str) and substep_run_id.strip():
                step_result_refs_by_substep[substep_run_id.strip()] = result_path

    for idx, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise RuntimeError(f"agent_graph edge must be object: index={idx}")
        parent_id = edge.get("parent_agent_run_id")
        child_id = edge.get("child_agent_run_id")
        if not isinstance(parent_id, str) or not parent_id.strip() or parent_id.strip() not in runs:
            raise RuntimeError(
                f"agent_graph edge parent_agent_run_id missing from agent_runs.jsonl: index={idx}"
            )
        if not isinstance(child_id, str) or not child_id.strip() or child_id.strip() not in runs:
            raise RuntimeError(
                f"agent_graph edge child_agent_run_id missing from agent_runs.jsonl: index={idx}"
            )

    for run_id, payload in runs.items():
        role = payload.get("agent_role")
        if not isinstance(role, str) or role not in {"step", "substep"}:
            continue
        status = payload.get("status")
        if not isinstance(status, str) or status.strip().lower() not in TERMINAL_STATUSES:
            raise RuntimeError(f"{role} agent_run_id must be terminal before pass: {run_id}")
        _validate_step_or_substep_launch_refs(repo_root, payload)
        node_key = payload.get("node_key")
        step = payload.get("step")
        if not isinstance(node_key, str) or not node_key.strip():
            raise RuntimeError(f"{role} node_key missing: {run_id}")
        if not isinstance(step, str) or not step.strip():
            raise RuntimeError(f"{role} step missing: {run_id}")
        node_safe = _node_key_to_safe(node_key.strip())
        step_token = step.strip().lower()
        if role == "step":
            result_path = root / "steps" / node_safe / step_token / run_id / "step_result.json"
            if not result_path.exists():
                raise RuntimeError(f"step_result.json missing for step agent_run_id={run_id}")
        else:
            if run_id not in step_result_refs_by_substep:
                raise RuntimeError(
                    f"step_result.json missing substep_agent_run_ids entry for substep agent_run_id={run_id}"
                )


def _validate_step_result_payload(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    agent_run_id: str,
    payload: dict[str, Any],
) -> None:
    step_token = step.strip().lower()
    if step_token not in {"plan", "generate", "tune"}:
        return
    status = payload.get("status")
    if not isinstance(status, str) or status.strip().lower() != "pass":
        return
    substep_run_ids = payload.get("substep_agent_run_ids")
    if not isinstance(substep_run_ids, list) or not substep_run_ids:
        raise ValueError(f"pass step_result for {step_token} requires non-empty substep_agent_run_ids")

    run_records = _load_run_records(_orchestration_root(repo_root, orchestration_id))
    required_outputs = payload.get("required_outputs")
    if not isinstance(required_outputs, list):
        raise ValueError("step_result.required_outputs must be list")
    declared_outputs = {item.strip() for item in required_outputs if isinstance(item, str) and item.strip()}

    substep_outputs: set[str] = set()
    for idx, substep_run_id in enumerate(substep_run_ids):
        if not isinstance(substep_run_id, str) or not substep_run_id.strip():
            raise ValueError(f"substep_agent_run_ids[{idx}] must be non-empty string")
        substep_record = run_records.get(substep_run_id.strip())
        if not isinstance(substep_record, dict):
            raise ValueError(f"missing substep run record: {substep_run_id}")
        substep_status = substep_record.get("status")
        if not isinstance(substep_status, str) or substep_status.strip().lower() != "pass":
            raise ValueError(f"substep {substep_run_id} must be pass before step_result can pass")
        output_refs = substep_record.get("output_refs")
        if not isinstance(output_refs, list) or not output_refs:
            raise ValueError(f"substep {substep_run_id} must publish non-empty output_refs")
        for output_ref in output_refs:
            if isinstance(output_ref, str) and output_ref.strip():
                substep_outputs.add(output_ref.strip())

    missing_outputs = sorted(ref for ref in declared_outputs if ref not in substep_outputs)
    if missing_outputs:
        raise ValueError(
            "step_result.required_outputs must be satisfied by substep output_refs: "
            + ", ".join(missing_outputs)
        )


def parse_feature_list(raw: str) -> dict[str, bool]:
    features: dict[str, bool] = {}
    for line in raw.splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        enabled = parts[-1].lower()
        if enabled not in {"true", "false"}:
            continue
        feature_name = parts[0].strip()
        if feature_name:
            features[feature_name] = enabled == "true"
    return features


def probe_execution_platform(
    *,
    backend: str,
    agent_command: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    backend_token = backend.strip().lower()
    if backend_token not in SUPPORTED_BACKENDS:
        raise ValueError(f"unsupported backend: {backend}")

    default_command = DEFAULT_BACKEND_COMMANDS[backend_token]
    command = agent_command.strip() if isinstance(agent_command, str) and agent_command.strip() else default_command
    for known_backend, known_command in DEFAULT_BACKEND_COMMANDS.items():
        if command != known_command:
            continue
        if known_backend != backend_token:
            raise ValueError(
                f"agent_command/backend mismatch: backend={backend_token} requires "
                f"{DEFAULT_BACKEND_COMMANDS[backend_token]} (or custom command), got {command}"
            )
        break

    version_proc = runner(
        [command, "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    features_proc = runner(
        [command, "features", "list"],
        text=True,
        capture_output=True,
        check=False,
    )

    features: dict[str, bool] = {}
    features_detail = features_proc.stdout.strip() or features_proc.stderr.strip()
    features_available = features_proc.returncode == 0
    multi_agent_enabled = False
    if features_proc.returncode == 0:
        features = parse_feature_list(features_proc.stdout)
        multi_agent_enabled = features.get("multi_agent") is True
    if backend_token == "cursor" and not multi_agent_enabled:
        # Cursor agent CLI may not expose `features list`.
        # In that case this fallback is a best-effort launchability probe, not
        # a hard guarantee that multi-agent launch will always succeed.
        # Launch-time live preflight in `record_launch` remains the fail-safe.
        help_proc = runner(
            [command, "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        if help_proc.returncode == 0:
            features_available = True
            multi_agent_enabled = True
            features = {"multi_agent": True}
            help_detail = help_proc.stdout.strip() or help_proc.stderr.strip()
            features_detail = "cursor backend multi_agent could not be confirmed from features list; fallback to --help succeeded"
            if help_detail:
                features_detail += f"\n{help_detail}"

    checks = [
        {
            "name": f"{backend_token}_version_available",
            "pass": version_proc.returncode == 0,
            "detail": version_proc.stdout.strip() or version_proc.stderr.strip(),
        },
        {
            "name": f"{backend_token}_features_available",
            "pass": features_available,
            "detail": features_detail,
        },
        {
            "name": "multi_agent_enabled",
            "pass": multi_agent_enabled,
            "detail": f"multi_agent={features.get('multi_agent')}",
        },
    ]

    can_launch_agents = version_proc.returncode == 0 and features_available and multi_agent_enabled
    return {
        "checked_at": _utc_now_iso(),
        "backend": backend_token,
        "probe_command": command,
        "agent_version": version_proc.stdout.strip(),
        "feature_states": features,
        "checks": checks,
        "can_launch_step_agents": can_launch_agents,
        "can_launch_substep_agents": can_launch_agents,
        "status": "pass" if can_launch_agents else "fail",
    }


def probe_codex_cli(
    codex_command: str = "codex",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    return probe_execution_platform(
        backend="codex",
        agent_command=codex_command,
        runner=runner,
    )


def init_orchestration(
    repo_root: Path,
    orchestration_id: str,
    *,
    spec_ref: str | None = None,
    dependency_ref: str | None = None,
    status: str = "running",
) -> dict[str, Any]:
    root = _orchestration_root(repo_root, orchestration_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "launches").mkdir(parents=True, exist_ok=True)
    (root / "agents").mkdir(parents=True, exist_ok=True)
    (root / "steps").mkdir(parents=True, exist_ok=True)

    meta = {
        "orchestration_id": orchestration_id,
        "status": status,
        "started_at": _utc_now_iso(),
    }
    if spec_ref:
        meta["spec_ref"] = spec_ref
    if dependency_ref:
        meta["dependency_ref"] = dependency_ref
    _write_json(root / "orchestration_meta.json", meta)

    graph_path = root / "agent_graph.json"
    if not graph_path.exists():
        _write_json(graph_path, {"edges": []})

    runs_path = root / "agent_runs.jsonl"
    if not runs_path.exists():
        runs_path.write_text("", encoding="utf-8")

    return meta


def write_preflight(repo_root: Path, orchestration_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    _validate_preflight_payload(payload)
    root = _orchestration_root(repo_root, orchestration_id)
    root.mkdir(parents=True, exist_ok=True)
    _write_json(root / "preflight.json", payload)
    return payload


def _load_graph(graph_path: Path) -> dict[str, Any]:
    if graph_path.exists():
        graph = _read_json(graph_path)
        if isinstance(graph, dict) and isinstance(graph.get("edges"), list):
            return graph
    return {"edges": []}


def record_launch(
    repo_root: Path,
    orchestration_id: str,
    *,
    parent_agent_run_id: str,
    child_agent_run_id: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    relation_type: str = "launch",
) -> dict[str, str]:
    try:
        _require_preflight_launchable(
            repo_root,
            orchestration_id,
            enforce_live_probe=True,
        )
    except RuntimeError:
        # Launch gate failure must terminate orchestration immediately.
        try:
            update_orchestration_status(
                repo_root,
                orchestration_id,
                status="fail",
            )
        except Exception:
            pass
        raise
    root = _orchestration_root(repo_root, orchestration_id)
    launches_root = root / "launches"
    launches_root.mkdir(parents=True, exist_ok=True)
    child_dialog_root = root / "agents" / child_agent_run_id / "dialogs"
    child_dialog_root.mkdir(parents=True, exist_ok=True)

    request_payload = dict(request_payload)
    request_payload.setdefault("orchestration_id", orchestration_id)
    request_payload.setdefault("agent_run_id", child_agent_run_id)
    request_payload.setdefault("parent_agent_run_id", parent_agent_run_id)
    request_payload = prepare_launch_request_payload(request_payload)
    response_payload = dict(response_payload)
    response_agent_session_id = _validate_response_agent_session_id(response_payload)
    response_payload.setdefault("agent_session_id", response_agent_session_id)

    _validate_launch_request_payload(request_payload)

    prompt_text = _extract_launch_prompt_text(request_payload)
    reply_text = _extract_launch_reply_text(response_payload)
    if not prompt_text.strip():
        raise ValueError("launch prompt text must be non-empty")
    if not reply_text.strip():
        raise ValueError("launch reply text must be non-empty")
    _validate_launch_prompt_text(request_payload, prompt_text)

    request_ref, response_ref = _launch_refs(orchestration_id, child_agent_run_id)
    prompt_ref, reply_ref = _launch_dialog_refs(orchestration_id, child_agent_run_id)
    child_request_ref, child_response_ref = _child_launch_refs(orchestration_id, child_agent_run_id)
    child_prompt_ref, child_reply_ref = _child_dialog_refs(orchestration_id, child_agent_run_id)
    request_payload.setdefault("launch_prompt_ref", prompt_ref)
    request_payload.setdefault("child_launch_request_ref", child_request_ref)
    request_payload.setdefault("child_launch_prompt_ref", child_prompt_ref)
    response_payload.setdefault("launch_reply_ref", reply_ref)
    response_payload.setdefault("child_launch_response_ref", child_response_ref)
    response_payload.setdefault("child_launch_reply_ref", child_reply_ref)

    request_path = launches_root / f"{child_agent_run_id}.request.json"
    response_path = launches_root / f"{child_agent_run_id}.response.json"
    prompt_path = launches_root / f"{child_agent_run_id}.prompt.txt"
    reply_path = launches_root / f"{child_agent_run_id}.reply.txt"
    child_request_path = child_dialog_root / "child.request.json"
    child_response_path = child_dialog_root / "child.response.json"
    child_prompt_path = child_dialog_root / "child.prompt.txt"
    child_reply_path = child_dialog_root / "child.reply.txt"
    _write_json(request_path, request_payload)
    _write_json(response_path, response_payload)
    _write_text(prompt_path, prompt_text)
    _write_text(reply_path, reply_text)
    _write_json(child_request_path, request_payload)
    _write_json(child_response_path, response_payload)
    _write_text(child_prompt_path, prompt_text)
    _write_text(child_reply_path, reply_text)

    graph_path = root / "agent_graph.json"
    graph = _load_graph(graph_path)
    edge = {
        "parent_agent_run_id": parent_agent_run_id,
        "child_agent_run_id": child_agent_run_id,
        "relation_type": relation_type,
    }
    if edge not in graph["edges"]:
        graph["edges"].append(edge)
    _write_json(graph_path, graph)

    return {
        "launch_request_ref": request_ref,
        "launch_response_ref": response_ref,
        "launch_prompt_ref": prompt_ref,
        "launch_reply_ref": reply_ref,
        "child_launch_request_ref": child_request_ref,
        "child_launch_response_ref": child_response_ref,
        "child_launch_prompt_ref": child_prompt_ref,
        "child_launch_reply_ref": child_reply_ref,
    }


def _read_existing_run_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    run_ids: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        item = json.loads(line)
        run_id = item.get("agent_run_id")
        if isinstance(run_id, str) and run_id.strip():
            run_ids.add(run_id.strip())
    return run_ids


def record_agent_run(
    repo_root: Path,
    orchestration_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    root = _orchestration_root(repo_root, orchestration_id)
    root.mkdir(parents=True, exist_ok=True)
    runs_path = root / "agent_runs.jsonl"

    agent_run_id = payload.get("agent_run_id")
    if not isinstance(agent_run_id, str) or not agent_run_id.strip():
        raise ValueError("agent_run_id must be non-empty string")
    agent_run_id = agent_run_id.strip()

    role = payload.get("agent_role") or payload.get("agent_type") or payload.get("role")
    role_token = role.strip().lower() if isinstance(role, str) and role.strip() else None
    if role_token is None:
        raise ValueError("agent_role must be non-empty string")
    if role_token in {"step", "substep"}:
        _require_preflight_launchable(
            repo_root,
            orchestration_id,
            enforce_live_probe=False,
        )

    existing = _read_existing_run_ids(runs_path)
    if agent_run_id in existing:
        raise ValueError(f"duplicate agent_run_id: {agent_run_id}")

    payload = dict(payload)
    payload["agent_run_id"] = agent_run_id
    payload["agent_role"] = role_token
    payload.setdefault("started_at", _utc_now_iso())

    if role_token in {"step", "substep"}:
        payload.setdefault("context_isolated", True)
        request_ref, response_ref = _launch_refs(orchestration_id, agent_run_id)
        prompt_ref, reply_ref = _launch_dialog_refs(orchestration_id, agent_run_id)
        payload.setdefault("launch_request_ref", request_ref)
        payload.setdefault("launch_response_ref", response_ref)
        payload.setdefault("launch_prompt_ref", prompt_ref)
        payload.setdefault("launch_reply_ref", reply_ref)
        _validate_step_or_substep_launch_refs(repo_root, payload)

    _validate_terminal_run_payload(repo_root, orchestration_id, payload)

    status = payload.get("status")
    if isinstance(status, str) and status.strip().lower() in TERMINAL_STATUSES:
        payload.setdefault("finished_at", _utc_now_iso())

    if role_token in {"step", "substep"}:
        launch_response_path = repo_root / payload["launch_response_ref"]
        launch_response_payload = _read_json(launch_response_path)
        if not isinstance(launch_response_payload, dict):
            raise ValueError("launch response must be json object")
        response_agent_session_id = _validate_response_agent_session_id(launch_response_payload)
        payload_agent_session_id = payload.get("agent_session_id")
        if not isinstance(payload_agent_session_id, str) or not payload_agent_session_id.strip():
            raise ValueError("agent_session_id must be non-empty string")
        if payload_agent_session_id.strip() != response_agent_session_id:
            raise ValueError(
                "agent_session_id must match child agent identifier in launch response"
            )

    dialogs_root = root / "agents" / agent_run_id / "dialogs"
    dialogs_root.mkdir(parents=True, exist_ok=True)
    result_ref, summary_ref = _agent_result_refs(orchestration_id, agent_run_id)
    payload.setdefault("agent_result_ref", result_ref)
    payload.setdefault("agent_summary_ref", summary_ref)
    summary_text = _extract_agent_summary_text(payload)
    _validate_agent_summary_text(payload, summary_text)
    _write_json(dialogs_root / "agent.result.json", payload)
    _write_text(dialogs_root / "agent.summary.txt", summary_text)

    with runs_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    return payload


def write_step_result(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    agent_run_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    _require_preflight_launchable(
        repo_root,
        orchestration_id,
        enforce_live_probe=False,
    )
    node_safe = _node_key_to_safe(node_key)
    step_token = step.strip().lower()
    root = _orchestration_root(repo_root, orchestration_id)
    result_path = root / "steps" / node_safe / step_token / agent_run_id / "step_result.json"

    result = dict(payload)
    result.setdefault("executor_agent_run_id", agent_run_id)
    result.setdefault("required_outputs", [])
    result.setdefault("failed_substeps", [])

    _validate_step_result_payload(
        repo_root,
        orchestration_id,
        node_key=node_key,
        step=step,
        agent_run_id=agent_run_id,
        payload=result,
    )

    _write_json(result_path, result)
    return result


def update_orchestration_status(
    repo_root: Path,
    orchestration_id: str,
    *,
    status: str,
) -> dict[str, Any]:
    if status == "pass":
        _require_preflight_launchable(
            repo_root,
            orchestration_id,
            enforce_live_probe=False,
        )
        _validate_orchestration_completion_for_pass(repo_root, orchestration_id)
    meta_path = _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(meta_path)
    meta = _read_json(meta_path)
    if not isinstance(meta, dict):
        raise ValueError(f"invalid orchestration_meta.json: {meta_path}")
    meta["status"] = status
    if status in TERMINAL_STATUSES:
        meta["finished_at"] = _utc_now_iso()
    _write_json(meta_path, meta)
    return meta


def _json_arg(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("json payload must be object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--repo-root", required=True)
    init_parser.add_argument("--orchestration-id", required=True)
    init_parser.add_argument("--spec-ref")
    init_parser.add_argument("--dependency-ref")
    init_parser.add_argument("--status", default="running")

    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--repo-root", required=True)
    preflight_parser.add_argument("--orchestration-id", required=True)
    preflight_parser.add_argument("--backend", default="codex", choices=sorted(SUPPORTED_BACKENDS))
    preflight_parser.add_argument("--agent-command")
    preflight_parser.add_argument("--codex-command", default="codex")

    launch_parser = subparsers.add_parser("record-launch")
    launch_parser.add_argument("--repo-root", required=True)
    launch_parser.add_argument("--orchestration-id", required=True)
    launch_parser.add_argument("--parent-agent-run-id", required=True)
    launch_parser.add_argument("--child-agent-run-id", required=True)
    launch_parser.add_argument("--request-json", required=True, type=_json_arg)
    launch_parser.add_argument("--response-json", required=True, type=_json_arg)
    launch_parser.add_argument("--relation-type", default="launch")

    run_parser = subparsers.add_parser("record-agent-run")
    run_parser.add_argument("--repo-root", required=True)
    run_parser.add_argument("--orchestration-id", required=True)
    run_parser.add_argument("--agent-run-json", required=True, type=_json_arg)

    step_parser = subparsers.add_parser("write-step-result")
    step_parser.add_argument("--repo-root", required=True)
    step_parser.add_argument("--orchestration-id", required=True)
    step_parser.add_argument("--node-key", required=True)
    step_parser.add_argument("--step", required=True)
    step_parser.add_argument("--agent-run-id", required=True)
    step_parser.add_argument("--result-json", required=True, type=_json_arg)

    status_parser = subparsers.add_parser("set-status")
    status_parser.add_argument("--repo-root", required=True)
    status_parser.add_argument("--orchestration-id", required=True)
    status_parser.add_argument("--status", required=True)

    args = parser.parse_args(argv)
    repo_root = Path(getattr(args, "repo_root")).resolve()

    if args.command == "init":
        result = init_orchestration(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            spec_ref=args.spec_ref,
            dependency_ref=args.dependency_ref,
            status=args.status,
        )
    elif args.command == "preflight":
        agent_command = args.agent_command
        if (
            (not isinstance(agent_command, str) or not agent_command.strip())
            and args.backend == "codex"
        ):
            # Keep backward compatibility only for codex backend.
            agent_command = args.codex_command
        result = write_preflight(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            payload=probe_execution_platform(
                backend=args.backend,
                agent_command=agent_command,
            ),
        )
    elif args.command == "record-launch":
        result = record_launch(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            parent_agent_run_id=args.parent_agent_run_id,
            child_agent_run_id=args.child_agent_run_id,
            request_payload=args.request_json,
            response_payload=args.response_json,
            relation_type=args.relation_type,
        )
    elif args.command == "record-agent-run":
        result = record_agent_run(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            payload=args.agent_run_json,
        )
    elif args.command == "write-step-result":
        result = write_step_result(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            node_key=args.node_key,
            step=args.step,
            agent_run_id=args.agent_run_id,
            payload=args.result_json,
        )
    else:
        result = update_orchestration_status(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            status=args.status,
        )

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
