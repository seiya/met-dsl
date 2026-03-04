#!/usr/bin/env python3
"""Validate workflow pipeline semantic anti-cheat rules under workspace/."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLACEHOLDER_TEXT_PATTERNS = (
    '"sample":"state_recorded"',
    '"dummy"',
    '"placeholder"',
)

SNAPSHOT_SCHEMA_FILE = "snapshot_schema.json"
FORBIDDEN_RUNNER_OUTPUTS = (
    "verdict.json",
    "aggregate_verdict.json",
    "summary.json",
    "trial_meta.json",
)
LLM_REVIEW_FILENAME = "semantic_review.json"
FORTRAN_IDENTIFIER_PATTERN = re.compile(r"[a-z_][a-z0-9_]*")
RAW_EVIDENCE_ARTIFACTS = {
    "metrics_basis.json",
    "execution_trace.json",
    "state_snapshots",
}
RAW_EVIDENCE_ALIASES = {
    "metrics_basis.json": "metrics_basis.json",
    "raw/metrics_basis.json": "metrics_basis.json",
    "execution_trace.json": "execution_trace.json",
    "raw/execution_trace.json": "execution_trace.json",
    "state_snapshots": "state_snapshots",
    "raw/state_snapshots": "state_snapshots",
    "raw/state_snapshots/": "state_snapshots",
}
FORTRAN_KEYWORDS = {
    "if",
    "then",
    "else",
    "endif",
    "do",
    "enddo",
    "call",
    "subroutine",
    "module",
    "contains",
    "intent",
    "in",
    "out",
    "inout",
    "real",
    "integer",
    "logical",
    "character",
    "type",
    "public",
    "private",
    "use",
    "only",
    "true",
    "false",
}
QUALITY_CHECK_ALLOWED_COMMANDS = {"make", "ctest", "pytest"}
FORBIDDEN_QUALITY_CHECK_EXECUTABLES = {"python", "python3", "pypy", "bash", "sh", "zsh"}
TEST_ID_HEADING_PATTERN = re.compile(r"^###\s+\d+-\d+\.\s+`([^`]+)`\s*$")
TEST_OUTCOME_VALUES = {"pass", "fail", "xfail", "skipped", "blocked"}
SHAPE_EXPR_PATTERN = re.compile(r"^\[\s*([^\]]+?)\s*\]$")


def _normalize_workspace_root_token(workspace_root: str) -> str:
    token = workspace_root.strip().replace("\\", "/")
    token = token.lstrip("./")
    while "//" in token:
        token = token.replace("//", "/")
    return token.rstrip("/")


def _normalize_node_key_token(raw: str) -> str:
    token = raw.strip()
    if "/" not in token:
        return token
    kind, body = token.split("/", 1)
    spec_id = body.split("@", 1)[0].strip()
    if not kind.strip() or not spec_id:
        return token
    return f"{kind.strip()}/{spec_id}"


def _split_fortran_names(raw: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for idx, ch in enumerate(raw):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif ch == "," and depth == 0:
            parts.append(raw[start:idx])
            start = idx + 1
    parts.append(raw[start:])

    names: list[str] = []
    for token in parts:
        part = token.strip().lower()
        if not part:
            continue
        part = re.sub(r"\(.*\)", "", part).strip()
        if FORTRAN_IDENTIFIER_PATTERN.fullmatch(part):
            names.append(part)
    return names


def _is_literal_like_expr(expr: str) -> bool:
    lowered = expr.strip().lower()
    if not lowered:
        return False
    if lowered in {".true.", ".false.", "true", "false"}:
        return True
    return bool(re.fullmatch(r"[0-9dDeE\.\+\-\*\/\(\)\s,_]+", lowered))


def _validate_problem_model_literal_outputs(
    execution: NodeExecution,
    model_file: Path,
    lowered: str,
    violations: list[str],
) -> None:
    if not execution.node_key.startswith("problem/"):
        return

    subroutine_pattern = re.compile(
        r"subroutine\s+([a-z_][a-z0-9_]*)\s*\((.*?)\)(.*?)end\s+subroutine",
        re.DOTALL,
    )
    intent_out_pattern = re.compile(r"intent\s*\(\s*out\s*\)\s*::\s*([^\n!]+)")

    for match in subroutine_pattern.finditer(lowered):
        sub_name = match.group(1)
        arg_names = set(_split_fortran_names(match.group(2)))
        body = match.group(3)

        out_vars: set[str] = set()
        for out_match in intent_out_pattern.finditer(body):
            out_vars.update(_split_fortran_names(out_match.group(1)))
        if not out_vars:
            continue

        assign_map: dict[str, list[str]] = {}
        for out_var in sorted(out_vars):
            exprs = re.findall(
                rf"\b{re.escape(out_var)}\s*=\s*([^\n!]+)",
                body,
            )
            if exprs:
                assign_map[out_var] = [expr.strip() for expr in exprs]

        if set(assign_map.keys()) != out_vars:
            continue

        all_literal = True
        input_dependent = False
        for out_var, exprs in assign_map.items():
            for expr in exprs:
                if not _is_literal_like_expr(expr):
                    all_literal = False
                    expr_ids = {
                        token
                        for token in FORTRAN_IDENTIFIER_PATTERN.findall(expr)
                        if token not in {"d", "e", "true", "false"}
                    }
                    if expr_ids & (arg_names - {out_var}):
                        input_dependent = True

        if all_literal and not input_dependent:
            violations.append(
                f"{model_file}: subroutine {sub_name} has literal-only assignments for all intent(out) vars"
            )


def _extract_identifiers(expr: str) -> set[str]:
    return {
        token
        for token in FORTRAN_IDENTIFIER_PATTERN.findall(expr.lower())
        if token not in FORTRAN_KEYWORDS
    }


def _assignment_records(body: str) -> list[tuple[str, set[str], int]]:
    records: list[tuple[str, set[str], int]] = []
    assign_pattern = re.compile(
        r"^\s*([a-z_][a-z0-9_]*(?:\s*\([^\n=]*\))?)\s*=\s*([^\n!]+)",
        re.MULTILINE,
    )
    for match in assign_pattern.finditer(body):
        lhs_expr = match.group(1)
        lhs_match = FORTRAN_IDENTIFIER_PATTERN.search(lhs_expr.lower())
        if lhs_match is None:
            continue
        lhs = lhs_match.group(0)
        rhs_ids = _extract_identifiers(match.group(2))
        records.append((lhs, rhs_ids, match.start()))
    return records


def _validate_problem_model_dependency_dataflow(
    execution: NodeExecution,
    model_file: Path,
    lowered: str,
    dep_spec_ids: list[str],
    required_sources: set[str],
    violations: list[str],
) -> None:
    if not execution.node_key.startswith("problem/"):
        return
    if not dep_spec_ids:
        return

    dep_prefixes = tuple(f"{spec_id.lower()}__" for spec_id in dep_spec_ids)
    if not dep_prefixes:
        return

    subroutine_pattern = re.compile(
        r"subroutine\s+([a-z_][a-z0-9_]*)\s*\((.*?)\)(.*?)end\s+subroutine",
        re.DOTALL,
    )
    intent_out_pattern = re.compile(r"intent\s*\(\s*out\s*\)\s*::\s*([^\n!]+)")

    for sub_match in subroutine_pattern.finditer(lowered):
        sub_name = sub_match.group(1)
        arg_names = set(_split_fortran_names(sub_match.group(2)))
        body = sub_match.group(3)
        assignments = _assignment_records(body)

        out_vars: set[str] = set()
        for out_match in intent_out_pattern.finditer(body):
            out_vars.update(_split_fortran_names(out_match.group(1)))
        if not out_vars:
            continue

        dep_output_candidates: set[str] = set()
        for callee, args_raw, call_pos in _iter_fortran_calls(body):
            if not any(callee.startswith(prefix) for prefix in dep_prefixes):
                continue
            call_vars = _split_fortran_names(args_raw)
            for var in call_vars:
                if var in arg_names:
                    continue
                assigned_before_call = any(
                    lhs == var and pos < call_pos for lhs, _, pos in assignments
                )
                if not assigned_before_call:
                    dep_output_candidates.add(var)

        if not dep_output_candidates:
            continue

        dependency_sources = set(out_vars)
        changed = True
        while changed:
            changed = False
            for lhs, rhs_ids, _ in assignments:
                if lhs not in dependency_sources:
                    continue
                for src in rhs_ids:
                    if src not in dependency_sources:
                        dependency_sources.add(src)
                        changed = True

        if dep_output_candidates.isdisjoint(dependency_sources):
            violations.append(
                f"{model_file}: subroutine {sub_name} does not propagate dependency operation outputs "
                f"to intent(out) dataflow (candidates={sorted(dep_output_candidates)})"
            )

        if required_sources and required_sources.isdisjoint(dependency_sources):
            violations.append(
                f"{model_file}: subroutine {sub_name} does not include required semantic sources "
                f"in intent(out) dataflow (required={sorted(required_sources)})"
            )


def _is_multidim_problem_node(execution: NodeExecution) -> bool:
    if not execution.node_key.startswith("problem/"):
        return False
    spec_id = _spec_id_from_node_key(execution.node_key)
    if spec_id is None:
        return False
    spec_id_l = spec_id.lower()
    return "2d" in spec_id_l or "3d" in spec_id_l


def _validate_problem_state_array_usage(
    repo_root: Path,
    execution: NodeExecution,
    model_file: Path,
    lowered: str,
    violations: list[str],
) -> None:
    if not _is_multidim_problem_node(execution):
        return

    contract = _derived_contract_for_execution(repo_root, execution)
    if not isinstance(contract, dict):
        return
    kernel_contract = contract.get("numerical_kernel_contract")
    if not isinstance(kernel_contract, dict):
        return

    raw_state_variables = kernel_contract.get("state_variables")
    if not isinstance(raw_state_variables, list):
        return

    state_names: list[str] = []
    for item in raw_state_variables:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            state_names.append(name.strip().lower())

    if not state_names:
        return

    subroutine_pattern = re.compile(
        r"subroutine\s+([a-z_][a-z0-9_]*)\s*\((.*?)\)(.*?)end\s+subroutine",
        re.DOTALL,
    )
    intent_out_pattern = re.compile(r"intent\s*\(\s*out\s*\)\s*::\s*([^\n!]+)")
    candidate_found = False
    for match in subroutine_pattern.finditer(lowered):
        sub_name = match.group(1)
        body = match.group(3)
        out_vars: set[str] = set()
        for out_match in intent_out_pattern.finditer(body):
            out_vars.update(_split_fortran_names(out_match.group(1)))
        if len(out_vars) < 3:
            continue
        candidate_found = True

        missing_array_refs: list[str] = []
        for name in sorted(set(state_names)):
            array_ref = re.search(rf"\b{re.escape(name)}\s*\(", body)
            if array_ref is None:
                missing_array_refs.append(name)
        if missing_array_refs:
            violations.append(
                f"{model_file}: subroutine {sub_name} must reference state arrays declared in numerical_kernel_contract ({missing_array_refs})"
            )

    if not candidate_found:
        return


def _validate_problem_metric_only_scalar_kernel(
    execution: NodeExecution,
    model_file: Path,
    lowered: str,
    violations: list[str],
) -> None:
    if not _is_multidim_problem_node(execution):
        return
    spec_id = _spec_id_from_node_key(execution.node_key) or execution.node_key

    subroutine_pattern = re.compile(
        r"subroutine\s+([a-z_][a-z0-9_]*)\s*\((.*?)\)(.*?)end\s+subroutine",
        re.DOTALL,
    )
    intent_out_pattern = re.compile(r"intent\s*\(\s*out\s*\)\s*::\s*([^\n!]+)")
    intent_in_or_inout_array_pattern = re.compile(
        r"intent\s*\(\s*(?:in|inout)\s*\)\s*::\s*[^\n]*\([^)]+\)"
    )
    do_loop_pattern = re.compile(r"^\s*do\s+[a-z_][a-z0-9_]*\s*=", re.MULTILINE)
    forall_pattern = re.compile(r"^\s*forall\s*\(", re.MULTILINE)

    for match in subroutine_pattern.finditer(lowered):
        sub_name = match.group(1)
        body = match.group(3)
        out_vars: set[str] = set()
        for out_match in intent_out_pattern.finditer(body):
            out_vars.update(_split_fortran_names(out_match.group(1)))
        if len(out_vars) < 5:
            continue

        has_array_inputs = bool(intent_in_or_inout_array_pattern.search(body))
        has_loop = bool(do_loop_pattern.search(body) or forall_pattern.search(body))
        if has_array_inputs or has_loop:
            continue

        violations.append(
            f"{model_file}: subroutine {sub_name} is metric-only scalar kernel for {spec_id}; "
            "2d/3d problem model must not derive many intent(out) metrics without array inputs or update loops"
        )


def _extract_first_diagnostics_block(lowered: str) -> str | None:
    start = -1
    for marker in ("/diagnostics.json", "'diagnostics.json'", "\"diagnostics.json\""):
        start = lowered.find(marker)
        if start >= 0:
            break
    if start < 0:
        return None

    close_idx = lowered.find("close(", start)
    if close_idx < 0:
        return lowered[start:]
    return lowered[start:close_idx]


def _iter_fortran_calls(text: str) -> list[tuple[str, str, int]]:
    calls: list[tuple[str, str, int]] = []
    call_start_pattern = re.compile(r"\bcall\s+([a-z_][a-z0-9_]*)\s*\(")
    for match in call_start_pattern.finditer(text):
        name = match.group(1).lower()
        start = match.start()
        open_pos = match.end() - 1
        depth = 1
        idx = open_pos + 1
        while idx < len(text) and depth > 0:
            ch = text[idx]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            idx += 1
        if depth == 0:
            args = text[open_pos + 1 : idx - 1]
        else:
            args = text[open_pos + 1 :]
        calls.append((name, args, start))
    return calls


def _extract_call_arg_vars(lowered: str) -> list[str]:
    for _, args_raw, _ in _iter_fortran_calls(lowered):
        names = _split_fortran_names(args_raw)
        if names:
            return names
    return []


def _strip_quoted_strings(text: str) -> str:
    no_single = re.sub(r"'(?:''|[^'])*'", "''", text)
    return re.sub(r"\"(?:\"\"|[^\"])*\"", "\"\"", no_single)


def _makefile_logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    buffer = ""
    for raw_line in text.splitlines():
        if raw_line.startswith("\t"):
            continue

        line_no_comment = raw_line.split("#", 1)[0].rstrip()
        if not line_no_comment.strip():
            continue

        chunk = line_no_comment.strip()
        if chunk.endswith("\\"):
            buffer += chunk[:-1].strip() + " "
            continue

        logical = (buffer + chunk).strip()
        buffer = ""
        if logical:
            lines.append(logical)

    if buffer.strip():
        lines.append(buffer.strip())
    return lines


def _normalize_make_token(token: str) -> str | None:
    cleaned = token.strip().rstrip("\\")
    if not cleaned:
        return None
    if "%" in cleaned:
        return None

    cleaned = re.sub(r"\$\([^)]+\)", "", cleaned)
    cleaned = re.sub(r"\$\{[^}]+\}", "", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    if cleaned.startswith("$"):
        return None

    name = Path(cleaned).name.lower()
    if not name or "$" in name:
        return None
    return name


def _parse_makefile_rules(makefile_text: str) -> dict[str, set[str]]:
    rules: dict[str, set[str]] = {}
    assignment_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*[:+?]?=")

    for line in _makefile_logical_lines(makefile_text):
        if assignment_pattern.match(line):
            continue
        if ":" not in line:
            continue

        target_raw, prereq_raw = line.split(":", 1)
        target_tokens = target_raw.split()
        if not target_tokens:
            continue

        prereq_expr = prereq_raw.split(";", 1)[0].replace("|", " ")
        prereq_tokens = prereq_expr.split()
        prereqs = {
            norm
            for token in prereq_tokens
            if (norm := _normalize_make_token(token)) is not None
        }

        for target_token in target_tokens:
            target = _normalize_make_token(target_token)
            if target is None:
                continue
            rules.setdefault(target, set()).update(prereqs)
    return rules


def _local_fortran_module_map(src_files: list[Path]) -> dict[str, str]:
    module_map: dict[str, str] = {}
    pattern = re.compile(r"^\s*module\s+(?!procedure\b)([a-z_][a-z0-9_]*)\b", re.MULTILINE)
    for src_file in src_files:
        text = src_file.read_text(encoding="utf-8", errors="ignore").lower()
        stem = src_file.stem.lower()
        for match in pattern.finditer(text):
            module_name = match.group(1)
            module_map.setdefault(module_name, stem)
    return module_map


def _fortran_source_module_deps(src_files: list[Path]) -> dict[str, set[str]]:
    module_map = _local_fortran_module_map(src_files)
    use_pattern = re.compile(
        r"^\s*use(?:\s*,\s*(?:intrinsic|non_intrinsic)\s*::|\s*::|\s+)?\s*([a-z_][a-z0-9_]*)\b",
        re.MULTILINE,
    )
    deps_by_stem: dict[str, set[str]] = {}
    for src_file in src_files:
        text = src_file.read_text(encoding="utf-8", errors="ignore").lower()
        stem = src_file.stem.lower()
        deps: set[str] = set()
        for match in use_pattern.finditer(text):
            used_module = match.group(1)
            provider_stem = module_map.get(used_module)
            if provider_stem is None or provider_stem == stem:
                continue
            deps.add(provider_stem)
        deps_by_stem[stem] = deps
    return deps_by_stem


def _validate_fortran_makefile_dependencies(generate_root: Path, violations: list[str]) -> None:
    if not generate_root.exists():
        return

    gen_dirs = sorted(d for d in generate_root.iterdir() if d.is_dir())
    for gen_dir in gen_dirs:
        src_dir = gen_dir / "src"
        if not src_dir.exists():
            continue

        src_files = sorted(
            p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() == ".f90"
        )
        if len(src_files) < 2:
            continue

        deps_by_stem = _fortran_source_module_deps(src_files)
        required_object_deps = {
            stem: deps for stem, deps in deps_by_stem.items() if deps
        }
        if not required_object_deps:
            continue

        makefile_path = src_dir / "Makefile"
        if not makefile_path.exists():
            violations.append(
                f"{makefile_path}: missing for fortran module dependency build"
            )
            continue

        rules = _parse_makefile_rules(
            makefile_path.read_text(encoding="utf-8", errors="ignore")
        )
        for stem, deps in sorted(required_object_deps.items()):
            object_target = f"{stem}.o"
            prereqs = rules.get(object_target)
            if prereqs is None:
                violations.append(
                    f"{makefile_path}: missing explicit object dependency rule ({object_target})"
                )
                continue

            for dep_stem in sorted(deps):
                dep_mod = f"{dep_stem}.mod"
                dep_obj = f"{dep_stem}.o"
                if dep_mod not in prereqs and dep_obj not in prereqs:
                    violations.append(
                        f"{makefile_path}: {object_target} missing prerequisite for used module ({dep_mod} or {dep_obj})"
                    )


def _validate_problem_runner_diagnostics_dependency(
    execution: NodeExecution,
    runner_file: Path,
    lowered: str,
    violations: list[str],
) -> None:
    if not execution.node_key.startswith("problem/"):
        return

    diagnostics_block = _extract_first_diagnostics_block(lowered)
    if diagnostics_block is None:
        return

    call_args = _extract_call_arg_vars(lowered)
    if not call_args:
        return

    diagnostics_no_strings = _strip_quoted_strings(diagnostics_block)
    referenced_args = [
        name
        for name in call_args
        if re.search(rf"\b{re.escape(name)}\b", diagnostics_no_strings)
    ]
    numeric_literal_count = len(
        re.findall(r"[-+]?\d+(?:\.\d+)?(?:d|e)?[-+]?\d*", diagnostics_block)
    )
    if not referenced_args and numeric_literal_count >= 5:
        violations.append(
            f"{runner_file}: diagnostics block does not reference model call arguments and appears constant-heavy"
        )


def _validate_problem_runner_nonphysical_casepath_input(
    execution: NodeExecution,
    runner_file: Path,
    lowered: str,
    violations: list[str],
) -> None:
    if not execution.node_key.startswith("problem/"):
        return

    if "get_command_argument(1,case_path)" in lowered.replace(" ", ""):
        call_args = _extract_call_arg_vars(lowered)
        if not call_args:
            return

        suspicious_inputs: set[str] = set()
        assign_pattern = re.compile(
            r"^\s*([a-z_][a-z0-9_]*)\s*=\s*([^\n!]+)",
            re.MULTILINE,
        )
        for match in assign_pattern.finditer(lowered):
            lhs = match.group(1).strip()
            rhs = match.group(2).lower()
            if "len_trim(case_path)" in rhs or "command_argument_count()" in rhs:
                suspicious_inputs.add(lhs)

        if suspicious_inputs.intersection(call_args):
            violations.append(
                f"{runner_file}: model call input depends on case_path length/argument count and is non-physical"
            )


@dataclass
class NodeExecution:
    node_key: str
    node_dir: Path
    exec_dir: Path
    pipeline_dir: Path


@dataclass
class NodeLineage:
    node_key: str
    pipeline_dir: Path
    plan_ref: str | None
    dependency_ref: str | None


@dataclass
class SourceFingerprint:
    node_key: str
    pipeline_dir: Path
    digest: str


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _node_executions(
    workspace_root: Path, pipeline_roots: list[Path] | None = None
) -> list[NodeExecution]:
    result: list[NodeExecution] = []
    targets = _pipeline_targets(workspace_root, pipeline_roots)

    def has_execution_artifacts(node_dir: Path) -> bool:
        markers = (
            node_dir / "diagnostics.json",
            node_dir / "perf.json",
            node_dir / "trial_meta.json",
            node_dir / "quality_check.json",
            node_dir / "raw" / "metrics_basis.json",
        )
        return any(path.exists() for path in markers)

    for pipeline_dir in targets:
        if not pipeline_dir.is_dir():
            continue
        execute_root = pipeline_dir / "execute"
        if not execute_root.exists():
            continue
        for exec_dir in sorted(execute_root.iterdir()):
            if not exec_dir.is_dir():
                continue
            for kind_dir in sorted(exec_dir.iterdir()):
                if not kind_dir.is_dir():
                    continue
                for spec_dir in sorted(kind_dir.iterdir()):
                    if not spec_dir.is_dir():
                        continue
                    if not has_execution_artifacts(spec_dir):
                        continue
                    node_key = f"{kind_dir.name}/{spec_dir.name}"
                    result.append(
                        NodeExecution(
                            node_key=node_key,
                            node_dir=spec_dir,
                            exec_dir=exec_dir,
                            pipeline_dir=pipeline_dir,
                        )
                    )
    return result


def _pipeline_targets(
    workspace_root: Path, pipeline_roots: list[Path] | None
) -> list[Path]:
    if pipeline_roots is None:
        pipelines_root = workspace_root / "pipelines"
        if not pipelines_root.exists():
            return []
        targets: list[Path] = []
        for node_safe_dir in sorted(pipelines_root.iterdir()):
            if not node_safe_dir.is_dir():
                continue
            for pipeline_dir in sorted(node_safe_dir.iterdir()):
                if pipeline_dir.is_dir():
                    targets.append(pipeline_dir)
        return targets
    deduped: list[Path] = []
    seen: set[Path] = set()
    for pipeline_dir in sorted(pipeline_roots):
        if pipeline_dir in seen:
            continue
        seen.add(pipeline_dir)
        deduped.append(pipeline_dir)
    return deduped


def _lineage_records(
    workspace_root: Path, pipeline_roots: list[Path] | None
) -> list[NodeLineage]:
    records: list[NodeLineage] = []
    for pipeline_dir in _pipeline_targets(workspace_root, pipeline_roots):
        lineage_path = pipeline_dir / "lineage.json"
        if not lineage_path.exists():
            continue
        try:
            lineage = _read_json(lineage_path)
        except json.JSONDecodeError:
            continue
        if not isinstance(lineage, dict):
            continue
        node_key = lineage.get("node_key")
        if not isinstance(node_key, str) or not node_key.strip():
            continue
        plan_ref = lineage.get("plan_ref")
        dep_ref = lineage.get("dependency_ref")
        records.append(
            NodeLineage(
                node_key=node_key.strip(),
                pipeline_dir=pipeline_dir,
                plan_ref=plan_ref if isinstance(plan_ref, str) else None,
                dependency_ref=dep_ref if isinstance(dep_ref, str) else None,
            )
        )
    return records


def _iter_command_ref_entries(node: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if "command_id" in node and (
            "command_log_ref" in node or "command_log_path" in node
        ):
            refs.append(node)
        for value in node.values():
            refs.extend(_iter_command_ref_entries(value))
    elif isinstance(node, list):
        for item in node:
            refs.extend(_iter_command_ref_entries(item))
    return refs


def _validate_trial_meta(repo_root: Path, execution: NodeExecution, violations: list[str]) -> None:
    trial_meta_path = execution.node_dir / "trial_meta.json"
    if not trial_meta_path.exists():
        violations.append(f"{trial_meta_path}: missing")
        return

    data = _read_json(trial_meta_path)

    process_trace_ref = data.get("process_trace_ref")
    if not isinstance(process_trace_ref, str) or not process_trace_ref.startswith("workspace/"):
        violations.append(
            f"{trial_meta_path}:process_trace_ref must start with workspace/"
        )
    else:
        trace_path = repo_root / process_trace_ref
        if not trace_path.exists():
            violations.append(f"{trial_meta_path}:process_trace_ref target not found ({process_trace_ref})")

    raw_refs = data.get("raw_artifact_refs")
    if not isinstance(raw_refs, list) or not raw_refs:
        violations.append(f"{trial_meta_path}:raw_artifact_refs must be non-empty list")
    else:
        for i, ref in enumerate(raw_refs):
            if not isinstance(ref, str) or not ref.startswith("workspace/"):
                violations.append(
                    f"{trial_meta_path}:raw_artifact_refs[{i}] must start with workspace/"
                )
                continue
            target = repo_root / ref
            if not target.exists():
                violations.append(
                    f"{trial_meta_path}:raw_artifact_refs[{i}] target not found ({ref})"
                )

    source_command_ref = data.get("source_command_ref")
    if source_command_ref is None:
        violations.append(f"{trial_meta_path}:source_command_ref missing")
        return

    for entry in _iter_command_ref_entries(source_command_ref):
        command_id = entry.get("command_id")
        log_ref = entry.get("command_log_ref") or entry.get("command_log_path")
        if not isinstance(command_id, str) or not command_id:
            violations.append(f"{trial_meta_path}:command_id invalid in source_command_ref")
            continue
        if not isinstance(log_ref, str):
            violations.append(f"{trial_meta_path}:command_log_ref/path invalid in source_command_ref")
            continue
        log_path = repo_root / log_ref if log_ref.startswith("workspace/") else Path(log_ref)
        if not log_path.exists():
            violations.append(f"{trial_meta_path}:command log missing ({log_ref})")
            continue
        found = False
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("command_id") == command_id:
                found = True
                break
        if not found:
            violations.append(
                f"{trial_meta_path}:command_id {command_id} not found in {log_ref}"
            )


def _validate_raw_evidence(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    state_snapshot_required = _state_snapshot_required(repo_root, execution)
    required_raw_evidence = _required_raw_evidence(repo_root, execution)
    (
        expected_state_variables,
        expected_time_variable,
        expected_time_shape_expr,
        required_snapshot_min_samples,
    ) = _state_snapshot_requirement_details(repo_root, execution)

    required = [
        execution.node_dir / "diagnostics.json",
        execution.node_dir / "perf.json",
        execution.node_dir / "quality_check.json",
    ]
    if "metrics_basis.json" in required_raw_evidence:
        required.append(execution.node_dir / "raw" / "metrics_basis.json")
    if "execution_trace.json" in required_raw_evidence:
        required.append(execution.node_dir / "raw" / "execution_trace.json")
    if state_snapshot_required or "state_snapshots" in required_raw_evidence:
        required.append(execution.node_dir / "raw" / "state_snapshots")
    for path in required:
        if not path.exists():
            violations.append(f"{path}: missing")

    snapshots_dir = execution.node_dir / "raw" / "state_snapshots"
    if snapshots_dir.exists() and snapshots_dir.is_dir():
        files = sorted(p for p in snapshots_dir.rglob("*") if p.is_file())
        if not files:
            violations.append(f"{snapshots_dir}: empty directory")
        else:
            schema_path = snapshots_dir / SNAPSHOT_SCHEMA_FILE
            snapshot_data_files = [p for p in files if p != schema_path]
            for snapshot in files:
                text = snapshot.read_text(encoding="utf-8", errors="ignore")
                compact = text.replace(" ", "").replace("\n", "")
                for patt in PLACEHOLDER_TEXT_PATTERNS:
                    if patt in compact:
                        violations.append(
                            f"{snapshot}: placeholder content detected ({patt})"
                        )

            if state_snapshot_required:
                if not schema_path.exists():
                    violations.append(
                        f"{schema_path}: missing for required state_snapshots evidence"
                    )
                else:
                    try:
                        schema_data = _read_json(schema_path)
                    except json.JSONDecodeError:
                        violations.append(
                            f"{schema_path}: invalid json"
                        )
                        schema_data = None

                    state_variables: list[str] = []
                    state_variable_shapes: dict[str, str] = {}
                    time_variable = ""
                    time_shape_expr = "scalar"
                    if isinstance(schema_data, dict):
                        raw_variables = schema_data.get("variables")
                        if isinstance(raw_variables, list):
                            for raw_variable in raw_variables:
                                if not isinstance(raw_variable, dict):
                                    continue
                                raw_name = raw_variable.get("name")
                                raw_shape_expr = raw_variable.get("shape_expr")
                                if (
                                    isinstance(raw_name, str)
                                    and raw_name.strip()
                                    and isinstance(raw_shape_expr, str)
                                    and raw_shape_expr.strip()
                                ):
                                    name = raw_name.strip()
                                    state_variables.append(name)
                                    state_variable_shapes[name] = _canonical_shape_expr(raw_shape_expr)

                        raw_state_vars = schema_data.get("state_variables")
                        raw_time_var = schema_data.get("time_variable")
                        if isinstance(raw_state_vars, list):
                            for item in raw_state_vars:
                                if isinstance(item, str) and item.strip():
                                    token = item.strip()
                                    state_variables.append(token)
                                    state_variable_shapes.setdefault(token, "[*]")
                        if isinstance(raw_time_var, str) and raw_time_var.strip():
                            time_variable = raw_time_var.strip()
                        raw_time_shape = schema_data.get("time_shape_expr")
                        if isinstance(raw_time_shape, str) and raw_time_shape.strip():
                            time_shape_expr = _canonical_shape_expr(raw_time_shape)
                    else:
                        violations.append(
                            f"{schema_path}: must be json object"
                        )

                    if not state_variables:
                        violations.append(
                            f"{schema_path}: variables/state_variables must be non-empty"
                        )
                    if not time_variable:
                        violations.append(
                            f"{schema_path}: time_variable must be non-empty string"
                        )

                    if not snapshot_data_files:
                        violations.append(
                            f"{snapshots_dir}: snapshot data file missing"
                        )
                    elif state_variables and time_variable:
                        missing_state_by_file: dict[str, list[str]] = {}
                        missing_time = set()
                        for snapshot in snapshot_data_files:
                            if snapshot.suffix.lower() != ".json":
                                continue
                            try:
                                data = _read_json(snapshot)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(data, dict):
                                continue
                            keys = set(data.keys())
                            missing_state = sorted(name for name in state_variables if name not in keys)
                            if missing_state:
                                missing_state_by_file[snapshot.name] = missing_state
                            if time_variable not in keys:
                                missing_time.add(snapshot.name)

                            for name, shape_expr in state_variable_shapes.items():
                                if name not in data:
                                    continue
                                value_shape = _infer_json_shape(data.get(name))
                                if value_shape is None:
                                    violations.append(
                                        f"{snapshot}:{name} has unsupported or ragged shape"
                                    )
                                    continue
                                if not _shape_matches_expr(shape_expr, value_shape):
                                    violations.append(
                                        f"{snapshot}:{name} shape {value_shape} does not match declared shape_expr {shape_expr}"
                                    )

                            if time_variable in data:
                                time_shape = _infer_json_shape(data.get(time_variable))
                                if time_shape is None:
                                    violations.append(
                                        f"{snapshot}:{time_variable} has unsupported or ragged shape"
                                    )
                                elif not _shape_matches_expr(time_shape_expr, time_shape):
                                    violations.append(
                                        f"{snapshot}:{time_variable} shape {time_shape} does not match declared time_shape_expr {time_shape_expr}"
                                    )
                        if missing_state_by_file:
                            violations.append(
                                f"{snapshots_dir}: declared state_variables missing in snapshot files ({missing_state_by_file})"
                            )
                        if missing_time:
                            violations.append(
                                f"{snapshots_dir}: declared time_variable missing in snapshots ({time_variable}, files={sorted(missing_time)})"
                            )

                    if expected_state_variables:
                        missing_required = set(expected_state_variables.keys()) - set(state_variables)
                        if missing_required:
                            violations.append(
                                f"{schema_path}: missing required state_variables from derived_contract ({sorted(missing_required)})"
                            )
                        for name, expected_shape in expected_state_variables.items():
                            declared_shape = state_variable_shapes.get(name)
                            if declared_shape is None:
                                continue
                            if _canonical_shape_expr(expected_shape) != _canonical_shape_expr(declared_shape):
                                violations.append(
                                    f"{schema_path}: variable {name} shape_expr must match derived_contract ({expected_shape})"
                                )

                    if expected_time_variable and expected_time_variable != time_variable:
                        violations.append(
                            f"{schema_path}: time_variable must match derived_contract ({expected_time_variable})"
                        )
                    if expected_time_variable and _canonical_shape_expr(expected_time_shape_expr) != _canonical_shape_expr(time_shape_expr):
                        violations.append(
                            f"{schema_path}: time_shape_expr must match derived_contract ({expected_time_shape_expr})"
                        )

                if len(snapshot_data_files) < required_snapshot_min_samples:
                    violations.append(
                        f"{snapshots_dir}: snapshot data files must be >= {required_snapshot_min_samples}"
                    )

    diagnostics_path = execution.node_dir / "diagnostics.json"
    metrics_basis_path = execution.node_dir / "raw" / "metrics_basis.json"
    if diagnostics_path.exists() and metrics_basis_path.exists():
        diagnostics = _read_json(diagnostics_path)
        metrics_basis = _read_json(metrics_basis_path)
        if _canonical_json(diagnostics) == _canonical_json(metrics_basis):
            violations.append(
                f"{metrics_basis_path}: must not be identical copy of diagnostics.json"
            )

    quality_path = execution.node_dir / "quality_check.json"
    if quality_path.exists():
        quality = _read_json(quality_path)
        checks = quality.get("checks", {})
        if not isinstance(checks, dict):
            violations.append(f"{quality_path}:checks must be object")
        else:
            if checks.get("verdict_available") is not True:
                violations.append(
                    f"{quality_path}:checks.verdict_available must be true"
                )
            if checks.get("diagnostics_match") is not True:
                violations.append(
                    f"{quality_path}:checks.diagnostics_match must be true"
                )
            if checks.get("verdict_match") is not True:
                violations.append(
                    f"{quality_path}:checks.verdict_match must be true"
                )
        if quality.get("status") != "pass":
            violations.append(f"{quality_path}:status must be pass")


def _validate_generate_outputs(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    generate_root = execution.pipeline_dir / "generate"
    if not generate_root.exists():
        violations.append(f"{generate_root}: missing")
        return

    model_files, expected_model_name = _node_model_files(generate_root, execution)
    if not model_files:
        if expected_model_name is None:
            violations.append(f"{generate_root}: model source not found")
        else:
            violations.append(
                f"{generate_root}: node model source not found ({expected_model_name})"
            )
        return

    dep_spec_ids = _component_dep_spec_ids(repo_root, execution)
    required_sources = _semantic_required_sources(repo_root, execution)

    for model_file in model_files:
        text = model_file.read_text(encoding="utf-8", errors="ignore")
        lowered = text.lower()

        if re.search(r"index\s*\(\s*case_id", lowered) and re.search(
            r"metrics\s*\(\s*\d+\s*\)", lowered
        ):
            violations.append(
                f"{model_file}: hardcoded case_id -> metrics assignment pattern detected"
            )

        assignments = re.findall(
            r"metrics\s*\(\s*\d+\s*\)\s*=\s*([^\n!]+)",
            lowered,
            flags=re.MULTILINE,
        )
        literal_like = 0
        for rhs in assignments:
            if re.search(r"[-+]?\d+(?:\.\d+)?(?:d|e)?[-+]?\d*", rhs):
                literal_like += 1
        if len(assignments) >= 6 and literal_like >= 6:
            violations.append(
                f"{model_file}: many literal metric assignments detected ({literal_like}/{len(assignments)})"
            )

        _validate_problem_model_literal_outputs(
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            violations=violations,
        )

        _validate_problem_model_dependency_dataflow(
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            dep_spec_ids=dep_spec_ids,
            required_sources=required_sources,
            violations=violations,
        )
        _validate_problem_metric_only_scalar_kernel(
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            violations=violations,
        )
        _validate_problem_state_array_usage(
            repo_root=repo_root,
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            violations=violations,
        )

    _validate_fortran_makefile_dependencies(
        generate_root=generate_root,
        violations=violations,
    )


def _dependency_resolved_for_execution(repo_root: Path, execution: NodeExecution) -> dict[str, Any] | None:
    lineage_path = execution.pipeline_dir / "lineage.json"
    if not lineage_path.exists():
        return None

    lineage = _read_json(lineage_path)
    dependency_ref = lineage.get("dependency_ref")
    if not isinstance(dependency_ref, str) or not dependency_ref.startswith("workspace/"):
        return None

    dep_path = repo_root / dependency_ref
    if not dep_path.exists():
        return None
    try:
        dep_data = _read_json(dep_path)
    except json.JSONDecodeError:
        return None
    return dep_data if isinstance(dep_data, dict) else None


def _plan_dir_for_execution(repo_root: Path, execution: NodeExecution) -> Path | None:
    lineage_path = execution.pipeline_dir / "lineage.json"
    if not lineage_path.exists():
        return None

    lineage = _read_json(lineage_path)
    plan_ref = lineage.get("plan_ref")
    if not isinstance(plan_ref, str) or not plan_ref.startswith("workspace/"):
        return None

    plan_dir = repo_root / plan_ref
    if not plan_dir.exists() or not plan_dir.is_dir():
        return None
    return plan_dir


def _derived_contract_for_execution(
    repo_root: Path, execution: NodeExecution
) -> dict[str, Any] | None:
    plan_dir = _plan_dir_for_execution(repo_root, execution)
    if plan_dir is None:
        return None

    contract_path = plan_dir / "derived_contract.json"
    if not contract_path.exists():
        return None

    try:
        data = _read_json(contract_path)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _derived_contract_path_for_execution(
    repo_root: Path, execution: NodeExecution
) -> Path | None:
    plan_dir = _plan_dir_for_execution(repo_root, execution)
    if plan_dir is None:
        return None
    return plan_dir / "derived_contract.json"


def _tests_path_from_contract(repo_root: Path, contract: dict[str, Any]) -> Path | None:
    source = contract.get("source")
    if not isinstance(source, dict):
        return None
    tests_ref = source.get("tests")
    if not isinstance(tests_ref, str) or not tests_ref.strip():
        return None

    tests_path = Path(tests_ref.strip())
    if not tests_path.is_absolute():
        tests_path = repo_root / tests_path
    return tests_path


def _tests_path_for_execution(repo_root: Path, execution: NodeExecution) -> Path | None:
    contract = _derived_contract_for_execution(repo_root, execution)
    if not isinstance(contract, dict):
        return None
    return _tests_path_from_contract(repo_root, contract)


def _parse_test_ids_from_tests_md(tests_path: Path) -> list[str]:
    test_ids: list[str] = []
    seen: set[str] = set()
    for raw_line in tests_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        match = TEST_ID_HEADING_PATTERN.match(line)
        if not match:
            continue
        test_id = match.group(1).strip()
        if not test_id or test_id in seen:
            continue
        seen.add(test_id)
        test_ids.append(test_id)
    return test_ids


def _find_command_log_record(
    repo_root: Path, command_id: str, log_ref: str
) -> dict[str, Any] | None:
    log_path = repo_root / log_ref if log_ref.startswith("workspace/") else Path(log_ref)
    if not log_path.exists():
        return None

    for raw_line in log_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("command_id") == command_id:
            return obj if isinstance(obj, dict) else None
    return None


def _parse_shape_expr(expr: str) -> tuple[bool, list[str], str]:
    token = expr.strip()
    if not token:
        return False, [], "shape_expr must be non-empty"
    if token.lower() == "scalar":
        return True, [], ""

    match = SHAPE_EXPR_PATTERN.fullmatch(token)
    if match is None:
        return False, [], "shape_expr must be scalar or [dim1,dim2,...]"

    dims: list[str] = []
    for raw_dim in match.group(1).split(","):
        dim = raw_dim.strip()
        if not dim:
            return False, [], "shape_expr has empty dimension token"
        dims.append(dim)
    return True, dims, ""


def _canonical_shape_expr(expr: str) -> str:
    ok, dims, _ = _parse_shape_expr(expr)
    if not ok:
        return expr.strip().lower()
    if not dims:
        return "scalar"
    return "[" + ",".join(dim.lower() for dim in dims) + "]"


def _infer_json_shape(value: Any) -> list[int] | None:
    if isinstance(value, (int, float, str, bool)) or value is None:
        return []
    if isinstance(value, list):
        if not value:
            return [0]
        first_shape = _infer_json_shape(value[0])
        if first_shape is None:
            return None
        for item in value[1:]:
            shape = _infer_json_shape(item)
            if shape is None or shape != first_shape:
                return None
        return [len(value), *first_shape]
    return None


def _shape_matches_expr(shape_expr: str, actual_shape: list[int]) -> bool:
    ok, dims, _ = _parse_shape_expr(shape_expr)
    if not ok:
        return False
    if not dims:
        return actual_shape == []
    if len(dims) != len(actual_shape):
        return False
    for expected_dim, actual_dim in zip(dims, actual_shape):
        token = expected_dim.strip().lower()
        if token == "*":
            continue
        if token.isdigit():
            if int(token) != actual_dim:
                return False
            continue
        if actual_dim < 0:
            return False
    return True


def _state_snapshot_requirement_details(
    repo_root: Path, execution: NodeExecution
) -> tuple[dict[str, str], str, str, int]:
    required_variables: dict[str, str] = {}
    required_time_variable = ""
    required_time_shape_expr = "scalar"
    min_samples = 1

    raw_requirements = _raw_requirements_for_execution(repo_root, execution)
    if not isinstance(raw_requirements, dict):
        return required_variables, required_time_variable, required_time_shape_expr, min_samples

    required_evidence = raw_requirements.get("required_evidence")
    if not isinstance(required_evidence, list):
        return required_variables, required_time_variable, required_time_shape_expr, min_samples

    for item in required_evidence:
        if not isinstance(item, dict):
            continue
        raw_artifact = item.get("artifact")
        if not isinstance(raw_artifact, str):
            continue
        artifact = _normalize_raw_evidence_artifact(raw_artifact)
        if artifact != "state_snapshots":
            continue
        if isinstance(item.get("required"), bool) and not item["required"]:
            return required_variables, required_time_variable, required_time_shape_expr, min_samples

        raw_min_samples = item.get("min_samples")
        if isinstance(raw_min_samples, int) and raw_min_samples >= 1:
            min_samples = raw_min_samples

        schema = item.get("schema")
        if isinstance(schema, dict):
            raw_variables = schema.get("variables")
            if isinstance(raw_variables, list):
                for variable in raw_variables:
                    if not isinstance(variable, dict):
                        continue
                    raw_name = variable.get("name")
                    raw_shape_expr = variable.get("shape_expr")
                    if (
                        isinstance(raw_name, str)
                        and raw_name.strip()
                        and isinstance(raw_shape_expr, str)
                        and raw_shape_expr.strip()
                    ):
                        required_variables[raw_name.strip()] = _canonical_shape_expr(raw_shape_expr)

            raw_state_vars = schema.get("state_variables")
            if isinstance(raw_state_vars, list):
                for token in raw_state_vars:
                    if isinstance(token, str) and token.strip():
                        required_variables.setdefault(token.strip(), "[*]")

            raw_time_var = schema.get("time_variable")
            if isinstance(raw_time_var, str) and raw_time_var.strip():
                required_time_variable = raw_time_var.strip()
            raw_time_shape = schema.get("time_shape_expr")
            if isinstance(raw_time_shape, str) and raw_time_shape.strip():
                required_time_shape_expr = _canonical_shape_expr(raw_time_shape)

        return required_variables, required_time_variable, required_time_shape_expr, min_samples

    return required_variables, required_time_variable, required_time_shape_expr, min_samples


def _normalize_raw_evidence_artifact(token: str) -> str | None:
    normalized = token.strip().lower().replace("\\", "/")
    return RAW_EVIDENCE_ALIASES.get(normalized)


def _raw_requirements_for_execution(
    repo_root: Path, execution: NodeExecution
) -> dict[str, Any] | None:
    contract = _derived_contract_for_execution(repo_root, execution)
    if not isinstance(contract, dict):
        return None

    raw_requirements = contract.get("raw_requirements")
    if not isinstance(raw_requirements, dict):
        return None
    return raw_requirements


def _required_raw_evidence(
    repo_root: Path, execution: NodeExecution
) -> set[str]:
    required: set[str] = {"metrics_basis.json", "execution_trace.json"}
    raw_requirements = _raw_requirements_for_execution(repo_root, execution)
    if not isinstance(raw_requirements, dict):
        return required

    required_evidence = raw_requirements.get("required_evidence")
    if isinstance(required_evidence, list):
        for item in required_evidence:
            if not isinstance(item, dict):
                continue
            raw_artifact = item.get("artifact")
            if not isinstance(raw_artifact, str):
                continue
            artifact = _normalize_raw_evidence_artifact(raw_artifact)
            if artifact is None:
                continue
            item_required = item.get("required")
            if item_required is False:
                required.discard(artifact)
            else:
                required.add(artifact)
        return required

    if raw_requirements.get("state_snapshot_required") is True:
        required.add("state_snapshots")
    elif raw_requirements.get("state_snapshot_required") is False:
        required.discard("state_snapshots")
    return required


def _state_snapshot_required(repo_root: Path, execution: NodeExecution) -> bool:
    default_required = False
    raw_requirements = _raw_requirements_for_execution(repo_root, execution)
    if not isinstance(raw_requirements, dict):
        return default_required

    required_evidence = raw_requirements.get("required_evidence")
    if isinstance(required_evidence, list):
        for item in required_evidence:
            if not isinstance(item, dict):
                continue
            raw_artifact = item.get("artifact")
            if not isinstance(raw_artifact, str):
                continue
            artifact = _normalize_raw_evidence_artifact(raw_artifact)
            if artifact != "state_snapshots":
                continue
            item_required = item.get("required")
            if isinstance(item_required, bool):
                return item_required
            return True

    value = raw_requirements.get("state_snapshot_required")
    if isinstance(value, bool):
        return value
    return default_required


def _semantic_required_sources(repo_root: Path, execution: NodeExecution) -> set[str]:
    contract = _derived_contract_for_execution(repo_root, execution)
    if not isinstance(contract, dict):
        return set()

    required: set[str] = set()

    semantic_dep = contract.get("semantic_dependency")
    if isinstance(semantic_dep, dict):
        raw_sources = semantic_dep.get("required_sources")
        if isinstance(raw_sources, list):
            for item in raw_sources:
                if not isinstance(item, str):
                    continue
                token = item.strip().lower()
                if FORTRAN_IDENTIFIER_PATTERN.fullmatch(token):
                    required.add(token)

    io_contract = contract.get("io_contract")
    if isinstance(io_contract, dict):
        outputs = io_contract.get("outputs")
        if isinstance(outputs, list):
            for item in outputs:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not isinstance(name, str):
                    continue
                token = name.strip().lower()
                if FORTRAN_IDENTIFIER_PATTERN.fullmatch(token):
                    required.add(token)
    return required


def _validate_derived_contract_schema(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    contract_path = _derived_contract_path_for_execution(repo_root, execution)
    if contract_path is None:
        violations.append(
            f"{execution.pipeline_dir / 'lineage.json'}: plan_ref missing; cannot resolve derived_contract.json"
        )
        return
    if not contract_path.exists():
        violations.append(f"{contract_path}: missing")
        return

    try:
        contract = _read_json(contract_path)
    except json.JSONDecodeError:
        violations.append(f"{contract_path}: invalid json")
        return

    if not isinstance(contract, dict):
        violations.append(f"{contract_path}: must be json object")
        return

    output_items: list[tuple[int, dict[str, Any]]] = []
    io_contract = contract.get("io_contract")
    if not isinstance(io_contract, dict):
        violations.append(f"{contract_path}:io_contract must be object")
    else:
        inputs = io_contract.get("inputs")
        if not isinstance(inputs, list):
            violations.append(f"{contract_path}:io_contract.inputs must be list")

        outputs = io_contract.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            violations.append(f"{contract_path}:io_contract.outputs must be non-empty list")
        elif isinstance(outputs, list):
            for idx, item in enumerate(outputs):
                if not isinstance(item, dict):
                    violations.append(
                        f"{contract_path}:io_contract.outputs[{idx}] must be object"
                    )
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    violations.append(
                        f"{contract_path}:io_contract.outputs[{idx}].name must be non-empty string"
                    )
                evidence_ref = item.get("evidence_ref")
                if not isinstance(evidence_ref, str) or not evidence_ref.strip():
                    violations.append(
                        f"{contract_path}:io_contract.outputs[{idx}].evidence_ref must be non-empty string"
                    )
                shape_expr = item.get("shape_expr")
                if shape_expr is not None and (
                    not isinstance(shape_expr, str) or not shape_expr.strip()
                ):
                    violations.append(
                        f"{contract_path}:io_contract.outputs[{idx}].shape_expr must be non-empty string when present"
                    )
                output_items.append((idx, item))

    raw_requirements = contract.get("raw_requirements")
    if not isinstance(raw_requirements, dict):
        violations.append(f"{contract_path}:raw_requirements must be object")
        return

    required_evidence = raw_requirements.get("required_evidence")
    if not isinstance(required_evidence, list) or not required_evidence:
        violations.append(
            f"{contract_path}:raw_requirements.required_evidence must be non-empty list"
        )
        return

    snapshot_required = False
    snapshot_variables: dict[str, str] = {}
    snapshot_time_variable = ""
    snapshot_time_shape_expr = "scalar"

    for idx, item in enumerate(required_evidence):
        if not isinstance(item, dict):
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}] must be object"
            )
            continue
        raw_artifact = item.get("artifact")
        if not isinstance(raw_artifact, str) or not raw_artifact.strip():
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].artifact must be non-empty string"
            )
            continue

        artifact = _normalize_raw_evidence_artifact(raw_artifact)
        if artifact is None:
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].artifact "
                f"must be one of {sorted(RAW_EVIDENCE_ARTIFACTS)}"
            )
            continue

        required_value = item.get("required")
        if required_value is not None and not isinstance(required_value, bool):
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].required must be bool when present"
            )

        min_samples = item.get("min_samples")
        if min_samples is not None and (
            not isinstance(min_samples, int) or min_samples < 1
        ):
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].min_samples must be integer >= 1 when present"
            )

        if artifact != "state_snapshots":
            continue

        if item.get("required") is not False:
            snapshot_required = True

        schema = item.get("schema")
        if schema is None:
            continue
        if not isinstance(schema, dict):
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].schema must be object"
            )
            continue

        raw_variables = schema.get("variables")
        if raw_variables is None and item.get("required") is not False:
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.variables must be non-empty list when state_snapshots is required"
            )
        elif raw_variables is not None:
            if not isinstance(raw_variables, list) or not raw_variables:
                violations.append(
                    f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.variables must be non-empty list when present"
                )
            else:
                for var_idx, variable in enumerate(raw_variables):
                    if not isinstance(variable, dict):
                        violations.append(
                            f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.variables[{var_idx}] must be object"
                        )
                        continue
                    raw_name = variable.get("name")
                    raw_shape_expr = variable.get("shape_expr")
                    if not isinstance(raw_name, str) or not raw_name.strip():
                        violations.append(
                            f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.variables[{var_idx}].name must be non-empty string"
                        )
                        continue
                    if not isinstance(raw_shape_expr, str) or not raw_shape_expr.strip():
                        violations.append(
                            f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.variables[{var_idx}].shape_expr must be non-empty string"
                        )
                        continue
                    ok_shape, _, shape_err = _parse_shape_expr(raw_shape_expr)
                    if not ok_shape:
                        violations.append(
                            f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.variables[{var_idx}].shape_expr invalid ({shape_err})"
                        )
                        continue
                    snapshot_variables[raw_name.strip()] = _canonical_shape_expr(raw_shape_expr)

        raw_state_vars = schema.get("state_variables")
        if raw_state_vars is not None:
            if not isinstance(raw_state_vars, list) or not all(
                isinstance(token, str) and token.strip() for token in raw_state_vars
            ):
                violations.append(
                    f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.state_variables must be non-empty string list when present"
                )
            else:
                for token in raw_state_vars:
                    if isinstance(token, str) and token.strip():
                        snapshot_variables.setdefault(token.strip(), "[*]")
        raw_time_var = schema.get("time_variable")
        if raw_time_var is not None and (
            not isinstance(raw_time_var, str) or not raw_time_var.strip()
        ):
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.time_variable must be non-empty string when present"
            )
        elif isinstance(raw_time_var, str) and raw_time_var.strip():
            snapshot_time_variable = raw_time_var.strip()

        raw_time_shape = schema.get("time_shape_expr")
        if raw_time_shape is not None and (
            not isinstance(raw_time_shape, str) or not raw_time_shape.strip()
        ):
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.time_shape_expr must be non-empty string when present"
            )
        elif isinstance(raw_time_shape, str) and raw_time_shape.strip():
            ok_shape, _, shape_err = _parse_shape_expr(raw_time_shape)
            if not ok_shape:
                violations.append(
                    f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.time_shape_expr invalid ({shape_err})"
                )
            else:
                snapshot_time_shape_expr = _canonical_shape_expr(raw_time_shape)

    if snapshot_required and not snapshot_variables:
        violations.append(
            f"{contract_path}:state_snapshots schema must declare variables with shape_expr when required"
        )
    if snapshot_required and not snapshot_time_variable:
        violations.append(
            f"{contract_path}:state_snapshots schema must declare time_variable when required"
        )
    snapshot_reference_variables = set(snapshot_variables)
    if snapshot_time_variable:
        snapshot_reference_variables.add(snapshot_time_variable)

    for idx, item in output_items:
        evidence_ref = item.get("evidence_ref")
        has_snapshot_ref = isinstance(evidence_ref, str) and "state_snapshots" in evidence_ref
        if has_snapshot_ref or not snapshot_required:
            continue

        raw_variables = item.get("raw_variables")
        if not isinstance(raw_variables, list) or not raw_variables:
            violations.append(
                f"{contract_path}:io_contract.outputs[{idx}].raw_variables must be non-empty list when evidence_ref is non-snapshot and state_snapshots is required"
            )
            continue
        unknown_variables: list[str] = []
        for var_idx, token in enumerate(raw_variables):
            if not isinstance(token, str) or not token.strip():
                violations.append(
                    f"{contract_path}:io_contract.outputs[{idx}].raw_variables[{var_idx}] must be non-empty string"
                )
                continue
            name = token.strip()
            if name not in snapshot_reference_variables:
                unknown_variables.append(name)
        if unknown_variables:
            violations.append(
                f"{contract_path}:io_contract.outputs[{idx}].raw_variables must reference declared state_snapshots variables/time_variable ({sorted(set(unknown_variables))})"
            )

    if _is_multidim_problem_node(execution):
        kernel_contract = contract.get("numerical_kernel_contract")
        if not isinstance(kernel_contract, dict):
            violations.append(
                f"{contract_path}:numerical_kernel_contract must be object for multidimensional problem node"
            )
        else:
            state_variables = kernel_contract.get("state_variables")
            if not isinstance(state_variables, list) or not state_variables:
                violations.append(
                    f"{contract_path}:numerical_kernel_contract.state_variables must be non-empty list"
                )
            else:
                for idx, item in enumerate(state_variables):
                    if not isinstance(item, dict):
                        violations.append(
                            f"{contract_path}:numerical_kernel_contract.state_variables[{idx}] must be object"
                        )
                        continue
                    name = item.get("name")
                    shape_expr = item.get("shape_expr")
                    if not isinstance(name, str) or not name.strip():
                        violations.append(
                            f"{contract_path}:numerical_kernel_contract.state_variables[{idx}].name must be non-empty string"
                        )
                    if not isinstance(shape_expr, str) or not shape_expr.strip():
                        violations.append(
                            f"{contract_path}:numerical_kernel_contract.state_variables[{idx}].shape_expr must be non-empty string"
                        )
                    elif not _parse_shape_expr(shape_expr)[0]:
                        violations.append(
                            f"{contract_path}:numerical_kernel_contract.state_variables[{idx}].shape_expr invalid"
                        )

            update_paths = kernel_contract.get("required_update_paths")
            if not isinstance(update_paths, list) or not update_paths or not all(
                isinstance(item, str) and item.strip() for item in update_paths
            ):
                violations.append(
                    f"{contract_path}:numerical_kernel_contract.required_update_paths must be non-empty string list"
                )

            diagnostics_from_state = kernel_contract.get("diagnostics_from_state")
            if diagnostics_from_state is not True:
                violations.append(
                    f"{contract_path}:numerical_kernel_contract.diagnostics_from_state must be true"
                )

            fallback_policy = kernel_contract.get("fallback_policy")
            if fallback_policy != "fail_closed":
                violations.append(
                    f"{contract_path}:numerical_kernel_contract.fallback_policy must be fail_closed"
                )

    _validate_test_evidence_requirements(
        repo_root=repo_root,
        contract_path=contract_path,
        contract=contract,
        snapshot_reference_variables=snapshot_reference_variables,
        snapshot_required=snapshot_required,
        violations=violations,
    )


def _validate_test_evidence_requirements(
    repo_root: Path,
    contract_path: Path,
    contract: dict[str, Any],
    snapshot_reference_variables: set[str],
    snapshot_required: bool,
    violations: list[str],
) -> None:
    tests_path = _tests_path_from_contract(repo_root, contract)
    if tests_path is None or not tests_path.exists():
        return

    test_ids = _parse_test_ids_from_tests_md(tests_path)
    if not test_ids:
        return

    raw_reqs = contract.get("test_evidence_requirements")
    if not isinstance(raw_reqs, list) or not raw_reqs:
        violations.append(f"{contract_path}:test_evidence_requirements must be non-empty list")
        return

    seen_test_ids: set[str] = set()
    mapped_test_ids: set[str] = set()
    for idx, item in enumerate(raw_reqs):
        if not isinstance(item, dict):
            violations.append(
                f"{contract_path}:test_evidence_requirements[{idx}] must be object"
            )
            continue
        raw_test_id = item.get("test_id")
        if not isinstance(raw_test_id, str) or not raw_test_id.strip():
            violations.append(
                f"{contract_path}:test_evidence_requirements[{idx}].test_id must be non-empty string"
            )
            continue
        test_id = raw_test_id.strip()
        if test_id in seen_test_ids:
            violations.append(
                f"{contract_path}:test_evidence_requirements has duplicated test_id ({test_id})"
            )
            continue
        seen_test_ids.add(test_id)
        mapped_test_ids.add(test_id)

        raw_variables = item.get("required_raw_variables")
        if not isinstance(raw_variables, list) or not raw_variables:
            violations.append(
                f"{contract_path}:test_evidence_requirements[{idx}].required_raw_variables must be non-empty list"
            )
            continue
        for var_idx, token in enumerate(raw_variables):
            if not isinstance(token, str) or not token.strip():
                violations.append(
                    f"{contract_path}:test_evidence_requirements[{idx}].required_raw_variables[{var_idx}] must be non-empty string"
                )
                continue
            name = token.strip()
            if snapshot_required and name not in snapshot_reference_variables:
                violations.append(
                    f"{contract_path}:test_evidence_requirements[{idx}].required_raw_variables[{var_idx}] must reference declared state_snapshots variable/time_variable ({name})"
                )

    expected = set(test_ids)
    missing = sorted(expected - mapped_test_ids)
    extra = sorted(mapped_test_ids - expected)
    if missing:
        violations.append(
            f"{contract_path}:test_evidence_requirements missing tests from tests.md ({missing})"
        )
    if extra:
        violations.append(
            f"{contract_path}:test_evidence_requirements has unknown test_id ({extra})"
        )


def _component_dep_spec_ids(repo_root: Path, execution: NodeExecution) -> list[str]:
    dep_data = _dependency_resolved_for_execution(repo_root, execution)
    if dep_data is None:
        return []

    direct_deps = dep_data.get("direct_deps")
    if not isinstance(direct_deps, list):
        return []

    result: list[str] = []
    for item in direct_deps:
        dep_token: str | None = None
        if isinstance(item, str):
            dep_token = item
        elif isinstance(item, dict):
            node_key = item.get("node_key")
            if isinstance(node_key, str):
                dep_token = node_key

        if not isinstance(dep_token, str):
            continue
        # Expected format: component/<spec_id>@<spec_version>
        if not dep_token.startswith("component/"):
            continue
        body = dep_token[len("component/") :]
        spec_id = body.split("@", 1)[0].strip()
        if spec_id:
            result.append(spec_id)
    return sorted(set(result))


def _dep_node_key_tokens(node: Any) -> list[str]:
    tokens: list[str] = []
    if isinstance(node, str):
        token = node.strip()
        if token:
            tokens.append(_normalize_node_key_token(token))
    elif isinstance(node, dict):
        raw = node.get("node_key")
        if isinstance(raw, str):
            token = raw.strip()
            if token:
                tokens.append(_normalize_node_key_token(token))
    return tokens


def _dependency_expected_node_keys(dep_data: dict[str, Any]) -> set[str]:
    expected: set[str] = set()

    all_nodes = dep_data.get("all_nodes")
    if isinstance(all_nodes, list):
        for item in all_nodes:
            expected.update(_dep_node_key_tokens(item))

    node_key = dep_data.get("node_key")
    if isinstance(node_key, str) and node_key.strip():
        expected.update(_dep_node_key_tokens(node_key))

    if not expected:
        for field in ("direct_deps", "transitive_deps"):
            deps = dep_data.get(field)
            if not isinstance(deps, list):
                continue
            for item in deps:
                expected.update(_dep_node_key_tokens(item))
        if isinstance(node_key, str) and node_key.strip():
            expected.update(_dep_node_key_tokens(node_key))

    return expected


def _dependency_run_token(dep_data: dict[str, Any]) -> str | None:
    resolved_at = dep_data.get("resolved_at")
    if isinstance(resolved_at, str) and resolved_at.strip():
        return resolved_at.strip()
    return None


def _spec_id_from_node_key(node_key: str) -> str | None:
    if "/" not in node_key:
        return None
    body = node_key.split("/", 1)[1]
    spec_id = body.split("@", 1)[0].strip()
    return spec_id or None


def _node_model_files(
    generate_root: Path, execution: NodeExecution
) -> tuple[list[Path], str | None]:
    spec_id = _spec_id_from_node_key(execution.node_key)
    if spec_id is None:
        return sorted(generate_root.glob("*/src/*_model.f90")), None

    expected_name = f"{spec_id}_model.f90"
    targets: list[Path] = []
    for gen_dir in sorted(d for d in generate_root.iterdir() if d.is_dir()):
        src_dir = gen_dir / "src"
        if not src_dir.exists():
            continue
        candidate = src_dir / expected_name
        if candidate.exists():
            targets.append(candidate)
    return targets, expected_name


def _validate_dependency_operation_usage(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    dep_spec_ids = _component_dep_spec_ids(repo_root, execution)
    if not dep_spec_ids:
        return

    generate_root = execution.pipeline_dir / "generate"
    if not generate_root.exists():
        return

    model_files, expected_model_name = _node_model_files(generate_root, execution)
    if not model_files:
        if expected_model_name is not None:
            violations.append(
                f"{generate_root}: node model source not found ({expected_model_name})"
            )
        return

    for model_file in model_files:
        text = model_file.read_text(encoding="utf-8", errors="ignore")
        lowered = text.lower()

        for spec_id in dep_spec_ids:
            spec_id_l = spec_id.lower()
            op_prefix = re.escape(spec_id_l + "__")
            module_name = re.escape(spec_id_l + "_model")

            if not re.search(rf"\buse\s+{module_name}\b", lowered):
                violations.append(
                    f"{model_file}: missing dependency module use ({spec_id}_model)"
                )

            if re.search(rf"\bsubroutine\s+{op_prefix}[a-z0-9_]*\b", lowered):
                violations.append(
                    f"{model_file}: dependency operation redefinition detected ({spec_id}__*)"
                )

            if not re.search(rf"\bcall\s+{op_prefix}[a-z0-9_]*\b", lowered):
                violations.append(
                    f"{model_file}: missing dependency operation call ({spec_id}__*)"
                )


def _validate_runner_outputs(execution: NodeExecution, violations: list[str]) -> None:
    generate_root = execution.pipeline_dir / "generate"
    runner_files = sorted(generate_root.glob("*/src/*_runner.f90"))
    if not runner_files:
        return

    for runner_file in runner_files:
        text = runner_file.read_text(encoding="utf-8", errors="ignore").lower()
        for output_name in FORBIDDEN_RUNNER_OUTPUTS:
            if output_name in text:
                violations.append(
                    f"{runner_file}: forbidden runner output write detected ({output_name})"
                )
        _validate_problem_runner_diagnostics_dependency(
            execution=execution,
            runner_file=runner_file,
            lowered=text,
            violations=violations,
        )
        _validate_problem_runner_nonphysical_casepath_input(
            execution=execution,
            runner_file=runner_file,
            lowered=text,
            violations=violations,
        )


def _validate_run_program_inputs(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    trial_meta_path = execution.node_dir / "trial_meta.json"
    if not trial_meta_path.exists():
        return

    data = _read_json(trial_meta_path)
    source_command_ref = data.get("source_command_ref")
    if source_command_ref is None:
        return

    for entry in _iter_command_ref_entries(source_command_ref):
        command_id = entry.get("command_id")
        log_ref = entry.get("command_log_ref") or entry.get("command_log_path")
        if not isinstance(command_id, str) or not isinstance(log_ref, str):
            continue

        matched = _find_command_log_record(
            repo_root=repo_root,
            command_id=command_id,
            log_ref=log_ref,
        )
        if matched is None:
            continue
        if matched.get("tool_name") != "run_program":
            continue

        command = matched.get("command")
        if not isinstance(command, list):
            continue

        has_case_resolved = any(
            isinstance(arg, str) and arg.endswith("case.resolved.yaml")
            for arg in command
        )
        if not has_case_resolved:
            violations.append(
                f"{trial_meta_path}:run_program command_id={command_id} must include case.resolved.yaml"
            )


def _validate_quality_check_commands(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    trial_meta_path = execution.node_dir / "trial_meta.json"
    if not trial_meta_path.exists():
        return

    data = _read_json(trial_meta_path)
    source_command_ref = data.get("source_command_ref")
    if source_command_ref is None:
        return

    for entry in _iter_command_ref_entries(source_command_ref):
        command_id = entry.get("command_id")
        log_ref = entry.get("command_log_ref") or entry.get("command_log_path")
        if not isinstance(command_id, str) or not isinstance(log_ref, str):
            continue

        matched = _find_command_log_record(
            repo_root=repo_root,
            command_id=command_id,
            log_ref=log_ref,
        )
        if matched is None:
            continue
        if matched.get("tool_name") != "run_quality_checks":
            continue

        command = matched.get("command")
        if not isinstance(command, list) or not command:
            violations.append(
                f"{trial_meta_path}:run_quality_checks command_id={command_id} must have non-empty command array"
            )
            continue

        normalized = [str(token).strip() for token in command if str(token).strip()]
        if not normalized:
            violations.append(
                f"{trial_meta_path}:run_quality_checks command_id={command_id} must have non-empty command array"
            )
            continue

        executable = Path(normalized[0]).name.lower()
        if executable in FORBIDDEN_QUALITY_CHECK_EXECUTABLES:
            violations.append(
                f"{trial_meta_path}:run_quality_checks command_id={command_id} uses forbidden executable ({executable})"
            )
            continue

        if any("quality_check.py" in token.lower() for token in normalized):
            violations.append(
                f"{trial_meta_path}:run_quality_checks command_id={command_id} must not execute quality_check.py directly"
            )
            continue

        if executable not in QUALITY_CHECK_ALLOWED_COMMANDS:
            allowed = sorted(QUALITY_CHECK_ALLOWED_COMMANDS)
            violations.append(
                f"{trial_meta_path}:run_quality_checks command_id={command_id} executable must be one of {allowed}"
            )
            continue

        if executable == "make":
            targets = {token.lower() for token in normalized[1:]}
            if "test" not in targets and "check" not in targets:
                violations.append(
                    f"{trial_meta_path}:run_quality_checks command_id={command_id} make command must include test/check target"
                )


def _validate_tests_verdict_summary_consistency(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    tests_path = _tests_path_for_execution(repo_root, execution)
    if tests_path is None or not tests_path.exists():
        return

    test_ids = _parse_test_ids_from_tests_md(tests_path)
    if not test_ids:
        violations.append(f"{tests_path}: test_id heading not found")
        return

    verdict_path = execution.node_dir / "verdict.json"
    summary_path = execution.node_dir / "summary.json"
    if not verdict_path.exists():
        violations.append(f"{verdict_path}: missing")
        return
    if not summary_path.exists():
        violations.append(f"{summary_path}: missing")
        return

    try:
        verdict = _read_json(verdict_path)
    except json.JSONDecodeError:
        violations.append(f"{verdict_path}: invalid json")
        return
    if not isinstance(verdict, dict):
        violations.append(f"{verdict_path}: must be json object")
        return

    per_test = verdict.get("per_test")
    if not isinstance(per_test, list) or not per_test:
        violations.append(f"{verdict_path}:per_test must be non-empty list")
        return

    status_by_test: dict[str, str] = {}
    duplicate_test_ids: set[str] = set()
    invalid_entries = False
    for idx, item in enumerate(per_test):
        if not isinstance(item, dict):
            violations.append(f"{verdict_path}:per_test[{idx}] must be object")
            invalid_entries = True
            continue
        raw_test_id = item.get("test_id")
        if not isinstance(raw_test_id, str) or not raw_test_id.strip():
            violations.append(f"{verdict_path}:per_test[{idx}].test_id must be non-empty string")
            invalid_entries = True
            continue
        test_id = raw_test_id.strip()
        raw_status = item.get("status")
        if raw_status is None:
            raw_status = item.get("outcome")
        if not isinstance(raw_status, str):
            violations.append(
                f"{verdict_path}:per_test[{idx}] must define status/outcome string"
            )
            invalid_entries = True
            continue
        status = raw_status.strip().lower()
        if status not in TEST_OUTCOME_VALUES:
            violations.append(
                f"{verdict_path}:per_test[{idx}].status/outcome must be one of {sorted(TEST_OUTCOME_VALUES)}"
            )
            invalid_entries = True
            continue

        if test_id in status_by_test:
            duplicate_test_ids.add(test_id)
        status_by_test[test_id] = status

    if duplicate_test_ids:
        violations.append(
            f"{verdict_path}:per_test has duplicated test_id entries ({sorted(duplicate_test_ids)})"
        )
    if invalid_entries:
        return

    expected_ids = set(test_ids)
    actual_ids = set(status_by_test.keys())
    missing = sorted(expected_ids - actual_ids)
    extra = sorted(actual_ids - expected_ids)
    if missing:
        violations.append(f"{verdict_path}:per_test missing test_id entries from tests.md ({missing})")
    if extra:
        violations.append(f"{verdict_path}:per_test has unknown test_id entries ({extra})")
    if missing or extra:
        return

    computed_counts = {key: 0 for key in TEST_OUTCOME_VALUES}
    for status in status_by_test.values():
        computed_counts[status] += 1

    try:
        summary = _read_json(summary_path)
    except json.JSONDecodeError:
        violations.append(f"{summary_path}: invalid json")
        return
    if not isinstance(summary, dict):
        violations.append(f"{summary_path}: must be json object")
        return

    counts = summary.get("counts")
    if not isinstance(counts, dict):
        violations.append(f"{summary_path}:counts must be object")
        return

    for key in ("pass", "fail", "xfail", "skipped"):
        value = counts.get(key)
        if not isinstance(value, int) or value < 0:
            violations.append(f"{summary_path}:counts.{key} must be integer >= 0")
            continue
        if value != computed_counts[key]:
            violations.append(
                f"{summary_path}:counts.{key} must equal verdict.per_test aggregate ({computed_counts[key]})"
            )

    blocked_value = counts.get("blocked")
    if blocked_value is not None:
        if not isinstance(blocked_value, int) or blocked_value < 0:
            violations.append(f"{summary_path}:counts.blocked must be integer >= 0 when present")
        elif blocked_value != computed_counts["blocked"]:
            violations.append(
                f"{summary_path}:counts.blocked must equal verdict.per_test aggregate ({computed_counts['blocked']})"
            )


def _validate_llm_semantic_review(
    repo_root: Path,
    execution: NodeExecution,
    violations: list[str],
    *,
    require_llm_review: bool,
) -> None:
    review_path = execution.node_dir / LLM_REVIEW_FILENAME
    if not review_path.exists():
        if require_llm_review:
            violations.append(f"{review_path}: missing")
        return

    try:
        data = _read_json(review_path)
    except json.JSONDecodeError:
        violations.append(f"{review_path}: invalid json")
        return

    if not isinstance(data, dict):
        violations.append(f"{review_path}: must be json object")
        return

    review_method = data.get("review_method")
    if review_method != "llm_semantic_review":
        violations.append(
            f"{review_path}:review_method must be llm_semantic_review"
        )

    decision = data.get("decision")
    if decision not in {"pass", "fail"}:
        violations.append(f"{review_path}:decision must be pass/fail")
    elif decision != "pass":
        violations.append(f"{review_path}:decision is fail")

    scope = data.get("scope")
    if not isinstance(scope, dict):
        violations.append(f"{review_path}:scope must be object")
        return

    for key in ("model_ref", "runner_ref"):
        ref = scope.get(key)
        if not isinstance(ref, str) or not ref.startswith("workspace/"):
            violations.append(
                f"{review_path}:scope.{key} must start with workspace/"
            )
            continue
        target = repo_root / ref
        if not target.exists():
            violations.append(
                f"{review_path}:scope.{key} target not found ({ref})"
            )

    raw_refs = scope.get("raw_refs")
    if not isinstance(raw_refs, list) or not raw_refs:
        violations.append(f"{review_path}:scope.raw_refs must be non-empty list")
    else:
        for idx, ref in enumerate(raw_refs):
            if not isinstance(ref, str) or not ref.startswith("workspace/"):
                violations.append(
                    f"{review_path}:scope.raw_refs[{idx}] must start with workspace/"
                )
                continue
            target = repo_root / ref
            if not target.exists():
                violations.append(
                    f"{review_path}:scope.raw_refs[{idx}] target not found ({ref})"
                )


def _source_fingerprint(execution: NodeExecution) -> SourceFingerprint | None:
    generate_root = execution.pipeline_dir / "generate"
    gen_dirs = sorted(d for d in generate_root.iterdir() if d.is_dir()) if generate_root.exists() else []
    if not gen_dirs:
        return None

    src_dir = gen_dirs[-1] / "src"
    if not src_dir.exists():
        return None

    hasher = hashlib.sha256()
    included = 0
    for path in sorted(src_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".o", ".mod", ".a", ".so"}:
            continue
        if path.name in {"simulate"}:
            continue
        if "bin" in path.parts:
            continue
        rel = path.relative_to(src_dir).as_posix()
        hasher.update(rel.encode("utf-8"))
        hasher.update(path.read_bytes())
        included += 1
    if included == 0:
        return None

    return SourceFingerprint(
        node_key=execution.node_key,
        pipeline_dir=execution.pipeline_dir,
        digest=hasher.hexdigest(),
    )


def _resolve_pipeline_roots(
    repo_root: Path, workspace_root: str, raw_values: list[str] | None
) -> list[Path] | None:
    if not raw_values:
        return None

    workspace_path = repo_root / workspace_root
    pipelines_path = workspace_path / "pipelines"
    roots: list[Path] = []
    for raw in raw_values:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(workspace_path.resolve())
        except ValueError:
            raise ValueError(
                f"pipeline_root must be under {workspace_path}: {candidate}"
            ) from None
        try:
            candidate.relative_to(pipelines_path.resolve())
        except ValueError:
            raise ValueError(
                f"pipeline_root must be under {pipelines_path}: {candidate}"
            ) from None
        roots.append(candidate)
    return roots


def validate(
    repo_root: Path,
    workspace_root: str,
    pipeline_roots: list[Path] | None = None,
    require_llm_review: bool = True,
) -> list[str]:
    violations: list[str] = []
    normalized_workspace_root = _normalize_workspace_root_token(workspace_root)
    if normalized_workspace_root != "workspace":
        return [f"workspace_root must be exactly 'workspace' (given: {workspace_root})"]

    workspace_path = repo_root / workspace_root
    if not workspace_path.exists():
        return [f"{workspace_path}: workspace root does not exist"]

    executions = _node_executions(workspace_path, pipeline_roots=pipeline_roots)
    if not executions:
        return [f"{workspace_path}/pipelines: no execution artifacts found"]

    source_hash_map: dict[str, list[SourceFingerprint]] = {}
    dep_contexts: list[tuple[NodeExecution, set[str], str | None]] = []
    lineage_contexts: list[tuple[NodeLineage, set[str], str | None]] = []
    lineages = _lineage_records(workspace_path, pipeline_roots)

    for execution in executions:
        _validate_derived_contract_schema(repo_root, execution, violations)
        _validate_trial_meta(repo_root, execution, violations)
        _validate_raw_evidence(repo_root, execution, violations)
        _validate_generate_outputs(repo_root, execution, violations)
        _validate_dependency_operation_usage(repo_root, execution, violations)
        _validate_runner_outputs(execution, violations)
        _validate_run_program_inputs(repo_root, execution, violations)
        _validate_quality_check_commands(repo_root, execution, violations)
        _validate_tests_verdict_summary_consistency(repo_root, execution, violations)
        _validate_llm_semantic_review(
            repo_root,
            execution,
            violations,
            require_llm_review=require_llm_review,
        )

        fp = _source_fingerprint(execution)
        if fp is not None:
            source_hash_map.setdefault(fp.digest, []).append(fp)
        dep_data = _dependency_resolved_for_execution(repo_root, execution)
        if isinstance(dep_data, dict):
            expected_nodes = _dependency_expected_node_keys(dep_data)
            if expected_nodes:
                dep_contexts.append(
                    (
                        execution,
                        expected_nodes,
                        _dependency_run_token(dep_data),
                    )
                )
    for lineage in lineages:
        if not lineage.dependency_ref or not lineage.dependency_ref.startswith("workspace/"):
            continue
        dep_path = repo_root / lineage.dependency_ref
        if not dep_path.exists():
            continue
        try:
            dep_data = _read_json(dep_path)
        except json.JSONDecodeError:
            continue
        if not isinstance(dep_data, dict):
            continue
        all_nodes = dep_data.get("all_nodes")
        if not isinstance(all_nodes, list) or not all_nodes:
            continue
        expected_nodes = _dependency_expected_node_keys(dep_data)
        if expected_nodes:
            lineage_contexts.append((lineage, expected_nodes, _dependency_run_token(dep_data)))

    scope_nodes = {_normalize_node_key_token(execution.node_key) for execution in executions}
    scope_nodes_by_token: dict[str, set[str]] = {}
    for execution, _, token in dep_contexts:
        if token is None:
            continue
        scope_nodes_by_token.setdefault(token, set()).add(_normalize_node_key_token(execution.node_key))

    seen_dag_violations: set[tuple[Path, str, tuple[str, ...]]] = set()
    for execution, expected_nodes, token in dep_contexts:
        if token is None:
            available_nodes = scope_nodes
            scope_label = "validation scope"
        else:
            available_nodes = scope_nodes_by_token.get(token, set())
            scope_label = f"resolved_at={token}"
        missing = sorted(expected_nodes - available_nodes)
        if missing:
            key = (execution.pipeline_dir, scope_label, tuple(missing))
            if key in seen_dag_violations:
                continue
            seen_dag_violations.add(key)
            violations.append(
                f"{execution.pipeline_dir / 'lineage.json'}: dependency DAG incomplete for {scope_label}; missing node workflows {missing}"
            )

    scope_lineage_nodes = {_normalize_node_key_token(item.node_key) for item in lineages}
    scope_lineage_nodes_by_token: dict[str, set[str]] = {}
    scope_plan_nodes = set()
    scope_plan_nodes_by_token: dict[str, set[str]] = {}

    for lineage, _, token in lineage_contexts:
        node_token = _normalize_node_key_token(lineage.node_key)
        if token is not None:
            scope_lineage_nodes_by_token.setdefault(token, set()).add(node_token)
        plan_ok = False
        if isinstance(lineage.plan_ref, str) and lineage.plan_ref.startswith("workspace/"):
            plan_path = repo_root / lineage.plan_ref
            plan_ok = plan_path.exists() and plan_path.is_dir()
        if plan_ok:
            scope_plan_nodes.add(node_token)
            if token is not None:
                scope_plan_nodes_by_token.setdefault(token, set()).add(node_token)

    seen_issue_violations: set[tuple[Path, str, tuple[str, ...], tuple[str, ...]]] = set()
    for lineage, expected_nodes, token in lineage_contexts:
        if token is None:
            available_pipeline_nodes = scope_lineage_nodes
            available_plan_nodes = scope_plan_nodes
            scope_label = "validation scope"
        else:
            available_pipeline_nodes = scope_lineage_nodes_by_token.get(token, set())
            available_plan_nodes = scope_plan_nodes_by_token.get(token, set())
            scope_label = f"resolved_at={token}"
        missing_pipeline_nodes = sorted(expected_nodes - available_pipeline_nodes)
        missing_plan_nodes = sorted(expected_nodes - available_plan_nodes)
        if not missing_pipeline_nodes and not missing_plan_nodes:
            continue
        key = (
            lineage.pipeline_dir,
            scope_label,
            tuple(missing_plan_nodes),
            tuple(missing_pipeline_nodes),
        )
        if key in seen_issue_violations:
            continue
        seen_issue_violations.add(key)
        if missing_plan_nodes:
            violations.append(
                f"{lineage.pipeline_dir / 'lineage.json'}: node plans not issued for {scope_label}; missing nodes {missing_plan_nodes}"
            )
        if missing_pipeline_nodes:
            violations.append(
                f"{lineage.pipeline_dir / 'lineage.json'}: node pipelines not issued for {scope_label}; missing nodes {missing_pipeline_nodes}"
            )

    for digest, items in sorted(source_hash_map.items()):
        node_keys = sorted({item.node_key for item in items})
        if len(node_keys) <= 1:
            continue
        pipelines = sorted(
            {
                item.pipeline_dir.relative_to(repo_root).as_posix()
                for item in items
            }
        )
        violations.append(
            "copy_based_artifact_reuse detected: "
            + f"digest={digest[:12]} node_keys={node_keys} pipelines={pipelines}"
        )

    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--workspace-root", default="workspace")
    parser.add_argument(
        "--pipeline-root",
        action="append",
        default=None,
        help=(
            "Optional pipeline directory to validate. "
            "Can be repeated. Path must be under workspace/."
        ),
    )
    parser.add_argument(
        "--allow-missing-llm-review",
        action="store_true",
        help="Allow missing semantic_review.json for legacy pipelines.",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    try:
        pipeline_roots = _resolve_pipeline_roots(
            repo_root=repo_root,
            workspace_root=args.workspace_root,
            raw_values=args.pipeline_root,
        )
    except ValueError as exc:
        print(f"pipeline semantic validation: FAIL\n- {exc}")
        return 1

    violations = validate(
        repo_root=repo_root,
        workspace_root=args.workspace_root,
        pipeline_roots=pipeline_roots,
        require_llm_review=not args.allow_missing_llm_review,
    )

    if violations:
        print("pipeline semantic validation: FAIL")
        for line in violations:
            print(f"- {line}")
        return 1

    print("pipeline semantic validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
