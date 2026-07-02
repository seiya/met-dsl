#!/usr/bin/env python3
"""Deterministic dependency-graph builder (host-authored sidecar).

Builds the *derived* dependency graph — ``all_nodes`` (each carrying a
``topo_level``) and ``transitive_deps`` (each carrying its ``via`` intermediate
path) — for a target node from ``<spec_ref>/deps.yaml`` resolved against
``spec/registry/spec_catalog.yaml``. This is a pure function of the on-disk
registry: the SAME closure ``tools/run_workflow.py --with-deps`` resolves.

The conductor authors ``<ir_ref>/dependency_graph.json`` from this builder at
Compile phase start (see ``workflow_conductor._write_dependency_graph``) instead
of trusting the LLM's ``compile.generate`` output for the derived graph, which
could mutate ``topo_level``, drop a ``transitive`` edge, or diverge the closure
from ``deps.yaml``. The IR keeps only the low-mutation directly-read
``direct_deps`` (with the semantic ``operations``); the derived structure is
correct-by-construction host output. This is the sister of the other host-author
precedents (``lineage.json`` / ``src/Makefile`` / D5 published-interface
injection); see ``docs/design/deterministic_followups.md``.

The builder deliberately does NOT carry ``direct_deps[].operations`` (a semantic
field with no host data source — it stays LLM-authored in the IR) and does NOT
apply the Build/Model-B ``L6`` diamond guard (a staging/Makefile concern
unrelated to graph structure; see ``workflow_conductor._dependency_closure_nodes``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def build_dependency_graph(
    repo_root: Path,
    *,
    target_spec_ref: str,
    target_node_key: str,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Build the derived dependency graph for ``target_node_key``.

    Returns ``(graph, error)``:
      - ``graph``: on success, the sidecar dict
        ``{node_key, all_nodes:[{node_key, topo_level}], transitive_deps:[{node_key, via:[...]}], generated_by}``.
        ``all_nodes`` includes the target itself (``topo_level`` = height = the
        longest downward path to a leaf; a leaf is ``0``). ``transitive_deps`` is
        ``all_nodes − {self} − direct`` — the host directly-required set is
        recoverable as ``{all_nodes} − {self} − {transitive_deps}``. Both lists
        are sorted deterministically (the sidecar is re-authored every compile,
        so byte reproducibility is required).
      - ``error``: ``None`` on success; else ``{reason, detail}`` — fail-closed
        with NO partial graph. Reasons mirror
        ``run_workflow._resolve_dependency_closure`` exactly (``dependency_cycle``
        / ``dependency_unresolvable`` / ``dependency_version_conflict`` /
        ``dependency_identity_conflict`` / ``dependency_deps_unreadable`` /
        ``dependency_deps_malformed`` / ``dependency_spec_ref_unresolved`` /
        ``spec_catalog_corrupt``).

    Edges come from the canonical runtime helpers (``_read_deps_yaml`` /
    ``_parse_dep_entries`` / ``_matching_dep_versions`` / ``resolve_spec_ref_for``
    / ``_load_spec_catalog``); a post-order DFS records the child edges (a node
    already on the DFS stack is a cycle). Each dependency node is version-pinned
    to the highest catalog version satisfying its requiring edge(s)
    (``matched[0]``); the target's own version comes from ``target_node_key``.
    """
    from tools.orchestration_runtime import (
        SpecCatalogCorruption,
        _load_spec_catalog,
        _matching_dep_versions,
        _parse_dep_entries,
        _read_deps_yaml,
        resolve_spec_ref_for,
    )

    # Lazily loaded once a dependency edge is actually encountered — a leaf
    # target (empty deps.yaml) needs no catalog, so a missing/corrupt registry
    # must not turn an otherwise-buildable leaf into a failure (matches
    # `_resolve_dependency_closure`).
    catalog_cache: dict[tuple[str, str], tuple[str, ...]] | None = None

    def _get_catalog() -> dict[tuple[str, str], tuple[str, ...]]:
        nonlocal catalog_cache
        if catalog_cache is None:
            catalog_cache = _load_spec_catalog(str(repo_root.resolve()))
        return catalog_cache

    # child edges (ordered, deduped), identity + version-set per spec_ref.
    children_by_ref: dict[str, list[str]] = {}
    kindid_by_ref: dict[str, tuple[str, str]] = {}
    matched_by_ref: dict[str, tuple[str, ...]] = {}
    visiting: set[str] = set()
    done: list[str] = []
    done_set: set[str] = set()
    error: dict[str, str] | None = None

    def visit(spec_ref: str) -> None:
        nonlocal error
        if error is not None or spec_ref in done_set:
            return
        if spec_ref in visiting:
            error = {
                "reason": "dependency_cycle",
                "detail": f"dependency cycle detected at {spec_ref}",
            }
            return
        visiting.add(spec_ref)
        kids = children_by_ref.setdefault(spec_ref, [])
        deps_doc = _read_deps_yaml(repo_root, spec_ref)
        if not isinstance(deps_doc, dict):
            error = {
                "reason": "dependency_deps_unreadable",
                "detail": f"{spec_ref}/deps.yaml is missing or unparseable",
            }
            return
        entries, well_formed = _parse_dep_entries(deps_doc)
        if not well_formed:
            error = {
                "reason": "dependency_deps_malformed",
                "detail": f"{spec_ref}/deps.yaml has a malformed dependency schema",
            }
            return
        for kind, sid, constraint in entries:
            try:
                matched = _matching_dep_versions(_get_catalog(), kind, sid, constraint)
            except SpecCatalogCorruption as exc:
                error = {"reason": "spec_catalog_corrupt", "detail": str(exc)}
                return
            if not matched:
                error = {
                    "reason": "dependency_unresolvable",
                    "detail": (
                        f"{kind}/{sid} constraint {constraint!r} has no matching "
                        "catalog version"
                    ),
                }
                return
            # `resolve_spec_ref_for` re-reads the catalog and raises
            # SpecCatalogCorruption on a missing/unparseable registry (a
            # TOCTOU vs the earlier `_get_catalog()` read). Catch it so the
            # builder ALWAYS returns a `{reason}` error and `_write_dependency_graph`
            # fail-closes the phase cleanly, rather than letting the exception
            # escape into the conductor's run_phase loop.
            try:
                dep_spec_ref = resolve_spec_ref_for(repo_root, kind, sid)
            except SpecCatalogCorruption as exc:
                error = {"reason": "spec_catalog_corrupt", "detail": str(exc)}
                return
            if not dep_spec_ref:
                error = {
                    "reason": "dependency_spec_ref_unresolved",
                    "detail": f"no unique spec directory in catalog for {kind}/{sid}",
                }
                return
            prior_kindid = kindid_by_ref.get(dep_spec_ref)
            if prior_kindid is not None and prior_kindid != (kind, sid):
                error = {
                    "reason": "dependency_identity_conflict",
                    "detail": (
                        f"{dep_spec_ref} required as both {prior_kindid} and "
                        f"{(kind, sid)}"
                    ),
                }
                return
            kindid_by_ref[dep_spec_ref] = (kind, sid)
            # Intersect the matching-version sets across edges — an empty
            # intersection means two edges pin incompatible ranges for the same
            # node (a genuine conflict, fail-closed).
            prior_versions = matched_by_ref.get(dep_spec_ref)
            if prior_versions is None:
                matched_by_ref[dep_spec_ref] = tuple(matched)
            else:
                matched_set = set(matched)
                intersection = tuple(v for v in prior_versions if v in matched_set)
                if not intersection:
                    error = {
                        "reason": "dependency_version_conflict",
                        "detail": (
                            f"{dep_spec_ref} ({kind}/{sid}) required with "
                            f"incompatible constraints: {prior_versions} vs {tuple(matched)}"
                        ),
                    }
                    return
                matched_by_ref[dep_spec_ref] = intersection
            if dep_spec_ref not in kids:
                kids.append(dep_spec_ref)
            visit(dep_spec_ref)
            if error is not None:
                return
        visiting.discard(spec_ref)
        done_set.add(spec_ref)
        done.append(spec_ref)

    visit(target_spec_ref)
    if error is not None:
        return None, error

    def node_key_of(ref: str) -> str:
        if ref == target_spec_ref:
            return target_node_key
        kind, sid = kindid_by_ref[ref]
        # Pin to the highest catalog version satisfying the requiring edge(s)
        # (`matched` is descending). This matches the closure driver's own node
        # label (`run_workflow.py`: `f"{kind}/{sid}@{node['spec_versions'][0]}"`),
        # so the sidecar names each closure node with the SAME version
        # `--with-deps` schedules and the pre_judge DAG readiness glob accepts.
        # (`_stage_dependency_sources` keys the staged path on this exact version;
        # its correctness therefore depends on the dependency's own pipeline being
        # built under the same version — a pre-existing run_workflow property, not
        # affected by whether this graph is host- or LLM-authored.)
        version = matched_by_ref[ref][0]
        return f"{kind}/{sid}@{version}"

    # topo_level = height: longest downward path to a leaf. Post-order memoized;
    # the closure is acyclic (cycles already fail-closed above).
    height_cache: dict[str, int] = {}

    def height(ref: str) -> int:
        if ref in height_cache:
            return height_cache[ref]
        kids = children_by_ref.get(ref, [])
        h = 0 if not kids else max(1 + height(k) for k in kids)
        height_cache[ref] = h
        return h

    all_nodes = [
        {"node_key": node_key_of(ref), "topo_level": height(ref)} for ref in done
    ]
    all_nodes.sort(key=lambda n: (n["topo_level"], n["node_key"]))

    self_nk = target_node_key
    direct_nks = {node_key_of(r) for r in children_by_ref.get(target_spec_ref, [])}

    def via_for(dest_ref: str) -> list[str]:
        # Lexicographically-smallest intermediate node_key sequence on any path
        # target -> ... -> dest (endpoints excluded). Deterministic for byte
        # reproducibility across re-authoring; the closure is small + acyclic so
        # enumerating simple paths is fine.
        best: tuple[str, ...] | None = None
        stack: list[str] = []

        def dfs(ref: str) -> None:
            nonlocal best
            stack.append(ref)
            if ref == dest_ref:
                intermediate = tuple(node_key_of(r) for r in stack[1:-1])
                if best is None or intermediate < best:
                    best = intermediate
            else:
                for k in children_by_ref.get(ref, []):
                    dfs(k)
            stack.pop()

        dfs(target_spec_ref)
        return list(best) if best is not None else []

    transitive: list[dict[str, Any]] = []
    for ref in done:
        nk = node_key_of(ref)
        if nk == self_nk or nk in direct_nks:
            continue
        transitive.append({"node_key": nk, "via": via_for(ref)})
    transitive.sort(key=lambda d: d["node_key"])

    graph = {
        "node_key": target_node_key,
        "all_nodes": all_nodes,
        "transitive_deps": transitive,
        "generated_by": "conductor",
    }
    return graph, None
