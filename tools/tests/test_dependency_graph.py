#!/usr/bin/env python3
"""Unit tests for tools/dependency_graph.build_dependency_graph.

The builder is a pure function of `deps.yaml` + `spec_catalog.yaml`; these tests
seed a synthetic registry on disk and assert the derived graph (all_nodes /
topo_level / transitive_deps / via) and the fail-closed error taxonomy.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.dependency_graph import build_dependency_graph
from tools.orchestration_runtime import _load_spec_catalog


def _write_catalog(repo_root: Path, entries: list[dict]) -> None:
    lines = ["catalog_version: 0.2.0", "updated_at: 2026-06-18", "specs:"]
    for e in entries:
        lines.append(f"  - spec_kind: {e['spec_kind']}")
        lines.append(f"    spec_id: {e['spec_id']}")
        lines.append(f"    spec_version: \"{e['spec_version']}\"")
        lines.append(f"    deps_path: {e['deps_path']}")
    (repo_root / "spec" / "registry").mkdir(parents=True, exist_ok=True)
    (repo_root / "spec" / "registry" / "spec_catalog.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _write_deps(repo_root: Path, spec_ref: str, spec_kind: str, spec_id: str,
                components: list[tuple[str, str]] | None = None,
                profiles: list[tuple[str, str]] | None = None) -> None:
    d = repo_root / spec_ref
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"spec_id: {spec_id}", f"spec_kind: {spec_kind}", "dependencies:"]
    lines.append("  components:")
    for cid, c in (components or []):
        lines.append(f"    - component_id: {cid}")
        lines.append(f"      version_constraint: \"{c}\"")
    if not components:
        lines[-1] = "  components: []"
    lines.append("  profiles:")
    for pid, c in (profiles or []):
        lines.append(f"    - profile_id: {pid}")
        lines.append(f"      version_constraint: \"{c}\"")
    if not profiles:
        lines[-1] = "  profiles: []"
    (d / "deps.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


class BuildDependencyGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        _load_spec_catalog.cache_clear()

    def tearDown(self) -> None:
        _load_spec_catalog.cache_clear()

    # --- chain: top -> mid -> base (base leaf) ---
    def _seed_chain(self, repo_root: Path) -> None:
        _write_catalog(repo_root, [
            {"spec_kind": "component", "spec_id": "top", "spec_version": "0.1.0",
             "deps_path": "spec/component/top/deps.yaml"},
            {"spec_kind": "component", "spec_id": "mid", "spec_version": "0.1.0",
             "deps_path": "spec/component/mid/deps.yaml"},
            {"spec_kind": "component", "spec_id": "base", "spec_version": "0.1.0",
             "deps_path": "spec/component/base/deps.yaml"},
        ])
        _write_deps(repo_root, "spec/component/top", "component", "top",
                    components=[("mid", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/mid", "component", "mid",
                    components=[("base", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/base", "component", "base")

    def test_chain_topo_levels_and_transitive_via(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._seed_chain(repo)
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(err)
            self.assertEqual(graph["node_key"], "component/top@0.1.0")
            self.assertEqual(graph["generated_by"], "conductor")
            # height: base=0, mid=1, top=2
            self.assertEqual(graph["all_nodes"], [
                {"node_key": "component/base@0.1.0", "topo_level": 0},
                {"node_key": "component/mid@0.1.0", "topo_level": 1},
                {"node_key": "component/top@0.1.0", "topo_level": 2},
            ])
            # base is transitive (reached via mid); mid is direct (not listed).
            self.assertEqual(graph["transitive_deps"], [
                {"node_key": "component/base@0.1.0", "via": ["component/mid@0.1.0"]},
            ])
            # host_direct reconstruction = all_nodes - self - transitive = {mid}
            all_nk = {n["node_key"] for n in graph["all_nodes"]}
            trans_nk = {d["node_key"] for d in graph["transitive_deps"]}
            self.assertEqual(all_nk - {"component/top@0.1.0"} - trans_nk,
                             {"component/mid@0.1.0"})

    def test_leaf_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # No catalog needed for a leaf (empty deps).
            _write_deps(repo, "spec/component/base", "component", "base")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/base",
                target_node_key="component/base@0.1.0")
            self.assertIsNone(err)
            self.assertEqual(graph["all_nodes"],
                             [{"node_key": "component/base@0.1.0", "topo_level": 0}])
            self.assertEqual(graph["transitive_deps"], [])

    # --- diamond: a -> b, c ; b -> d ; c -> d ; d leaf ---
    def _seed_diamond(self, repo_root: Path) -> None:
        _write_catalog(repo_root, [
            {"spec_kind": "problem", "spec_id": "a", "spec_version": "0.3.0",
             "deps_path": "spec/problem/a/deps.yaml"},
            {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
             "deps_path": "spec/component/b/deps.yaml"},
            {"spec_kind": "component", "spec_id": "c", "spec_version": "0.1.0",
             "deps_path": "spec/component/c/deps.yaml"},
            {"spec_kind": "component", "spec_id": "d", "spec_version": "0.1.0",
             "deps_path": "spec/component/d/deps.yaml"},
        ])
        _write_deps(repo_root, "spec/problem/a", "problem", "a",
                    components=[("b", ">=0.1.0 <1.0.0"), ("c", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/b", "component", "b",
                    components=[("d", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/c", "component", "c",
                    components=[("d", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/d", "component", "d")

    def test_diamond_via_is_lex_min_and_l6_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._seed_diamond(repo)
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/problem/a",
                target_node_key="problem/a@0.3.0")
            self.assertIsNone(err)
            # heights: d=0, b=1, c=1, a=2
            self.assertEqual(graph["all_nodes"], [
                {"node_key": "component/d@0.1.0", "topo_level": 0},
                {"node_key": "component/b@0.1.0", "topo_level": 1},
                {"node_key": "component/c@0.1.0", "topo_level": 1},
                {"node_key": "problem/a@0.3.0", "topo_level": 2},
            ])
            # b, c are direct; only d is transitive. via = lex-min of
            # [b] vs [c] -> [component/b@0.1.0]. (Builder does not raise L6.)
            self.assertEqual(graph["transitive_deps"], [
                {"node_key": "component/d@0.1.0", "via": ["component/b@0.1.0"]},
            ])

    def test_profile_and_component_mixed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "problem", "spec_id": "p", "spec_version": "0.1.0",
                 "deps_path": "spec/problem/p/deps.yaml"},
                {"spec_kind": "profile", "spec_id": "pr", "spec_version": "0.2.0",
                 "deps_path": "spec/profile/pr/deps.yaml"},
                {"spec_kind": "component", "spec_id": "co", "spec_version": "0.1.0",
                 "deps_path": "spec/component/co/deps.yaml"},
            ])
            _write_deps(repo, "spec/problem/p", "problem", "p",
                        components=[("co", ">=0.1.0")], profiles=[("pr", ">=0.2.0")])
            _write_deps(repo, "spec/profile/pr", "profile", "pr")
            _write_deps(repo, "spec/component/co", "component", "co")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/problem/p",
                target_node_key="problem/p@0.1.0")
            self.assertIsNone(err)
            self.assertEqual({n["node_key"] for n in graph["all_nodes"]},
                             {"problem/p@0.1.0", "profile/pr@0.2.0", "component/co@0.1.0"})
            # both direct -> no transitive
            self.assertEqual(graph["transitive_deps"], [])

    def test_version_pins_highest_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "component", "spec_id": "top", "spec_version": "0.1.0",
                 "deps_path": "spec/component/top/deps.yaml"},
                {"spec_kind": "component", "spec_id": "base", "spec_version": "0.1.0",
                 "deps_path": "spec/component/base/deps.yaml"},
                {"spec_kind": "component", "spec_id": "base", "spec_version": "0.2.0",
                 "deps_path": "spec/component/base/deps.yaml"},
            ])
            _write_deps(repo, "spec/component/top", "component", "top",
                        components=[("base", ">=0.1.0 <1.0.0")])
            _write_deps(repo, "spec/component/base", "component", "base")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(err)
            self.assertIn({"node_key": "component/base@0.2.0", "topo_level": 0},
                          graph["all_nodes"])

    # --- error taxonomy ---
    def test_cycle_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                 "deps_path": "spec/component/b/deps.yaml"},
                {"spec_kind": "component", "spec_id": "c", "spec_version": "0.1.0",
                 "deps_path": "spec/component/c/deps.yaml"},
            ])
            _write_deps(repo, "spec/component/b", "component", "b",
                        components=[("c", ">=0.1.0")])
            _write_deps(repo, "spec/component/c", "component", "c",
                        components=[("b", ">=0.1.0")])
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/b",
                target_node_key="component/b@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_cycle")

    def test_unresolvable_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write_catalog(repo, [
                {"spec_kind": "component", "spec_id": "top", "spec_version": "0.1.0",
                 "deps_path": "spec/component/top/deps.yaml"},
                {"spec_kind": "component", "spec_id": "base", "spec_version": "0.1.0",
                 "deps_path": "spec/component/base/deps.yaml"},
            ])
            _write_deps(repo, "spec/component/top", "component", "top",
                        components=[("base", ">=9.0.0")])
            _write_deps(repo, "spec/component/base", "component", "base")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_unresolvable")

    def test_version_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # top -> base(==0.1.0); top -> mid -> base(==0.2.0): incompatible pins.
            _write_catalog(repo, [
                {"spec_kind": "component", "spec_id": "top", "spec_version": "0.1.0",
                 "deps_path": "spec/component/top/deps.yaml"},
                {"spec_kind": "component", "spec_id": "mid", "spec_version": "0.1.0",
                 "deps_path": "spec/component/mid/deps.yaml"},
                {"spec_kind": "component", "spec_id": "base", "spec_version": "0.1.0",
                 "deps_path": "spec/component/base/deps.yaml"},
                {"spec_kind": "component", "spec_id": "base", "spec_version": "0.2.0",
                 "deps_path": "spec/component/base/deps.yaml"},
            ])
            _write_deps(repo, "spec/component/top", "component", "top",
                        components=[("base", "==0.1.0"), ("mid", ">=0.1.0")])
            _write_deps(repo, "spec/component/mid", "component", "mid",
                        components=[("base", "==0.2.0")])
            _write_deps(repo, "spec/component/base", "component", "base")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_version_conflict")

    def test_malformed_deps_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            d = repo / "spec/component/top"
            d.mkdir(parents=True)
            # Unknown key -> malformed schema.
            (d / "deps.yaml").write_text(
                "spec_id: top\nspec_kind: component\ndependencies:\n  widgets: []\n",
                encoding="utf-8")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_deps_malformed")

    def test_spec_ref_unresolved_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # Catalog has base's version but no deps_path/controlled_spec_path -> no spec dir.
            (repo / "spec" / "registry").mkdir(parents=True, exist_ok=True)
            (repo / "spec" / "registry" / "spec_catalog.yaml").write_text(
                "catalog_version: 0.2.0\nupdated_at: 2026-06-18\nspecs:\n"
                "  - spec_kind: component\n    spec_id: top\n    spec_version: \"0.1.0\"\n"
                "    deps_path: spec/component/top/deps.yaml\n"
                "  - spec_kind: component\n    spec_id: base\n    spec_version: \"0.1.0\"\n",
                encoding="utf-8")
            _write_deps(repo, "spec/component/top", "component", "top",
                        components=[("base", ">=0.1.0")])
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_spec_ref_unresolved")

    def test_identity_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # top -> component/shared and mid -> profile/shared, both resolving to the SAME
            # spec dir (deps_path) but different (kind, id): an identity conflict.
            (repo / "spec" / "registry").mkdir(parents=True, exist_ok=True)
            (repo / "spec" / "registry" / "spec_catalog.yaml").write_text(
                "catalog_version: 0.2.0\nupdated_at: 2026-06-18\nspecs:\n"
                "  - spec_kind: component\n    spec_id: top\n    spec_version: \"0.1.0\"\n"
                "    deps_path: spec/component/top/deps.yaml\n"
                "  - spec_kind: component\n    spec_id: mid\n    spec_version: \"0.1.0\"\n"
                "    deps_path: spec/shared/deps.yaml\n"
                "  - spec_kind: profile\n    spec_id: mid\n    spec_version: \"0.1.0\"\n"
                "    deps_path: spec/shared/deps.yaml\n",
                encoding="utf-8")
            _write_deps(repo, "spec/component/top", "component", "top",
                        components=[("mid", ">=0.1.0")], profiles=[("mid", ">=0.1.0")])
            _write_deps(repo, "spec/shared", "component", "mid")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_identity_conflict")

    def test_catalog_corrupt_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # Non-leaf target (has a dep edge) but NO catalog on disk -> SpecCatalogCorruption.
            _write_deps(repo, "spec/component/top", "component", "top",
                        components=[("base", ">=0.1.0")])
            _write_deps(repo, "spec/component/base", "component", "base")
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/top",
                target_node_key="component/top@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "spec_catalog_corrupt")

    def test_catalog_corrupt_on_resolve_spec_ref_fails_closed(self) -> None:
        # The catalog resolves versions (cached) but is deleted before resolve_spec_ref_for
        # re-reads it -> SpecCatalogCorruption must be caught and returned as an error, not
        # escape as an uncaught exception into the conductor.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._seed_chain(repo)
            import tools.orchestration_runtime as ort
            from tools.orchestration_runtime import SpecCatalogCorruption

            def _boom(*a, **k):
                raise SpecCatalogCorruption("catalog vanished mid-traversal")

            orig = ort.resolve_spec_ref_for
            ort.resolve_spec_ref_for = _boom
            try:
                graph, err = build_dependency_graph(
                    repo, target_spec_ref="spec/component/top",
                    target_node_key="component/top@0.1.0")
            finally:
                ort.resolve_spec_ref_for = orig
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "spec_catalog_corrupt")

    def test_include_via_false_preserves_the_node_sets(self) -> None:
        """The R6-lite freshness comparison reads the NODE SETS (`all_nodes` + the
        `transitive_deps` membership) but never the `via` paths, whose enumeration is
        exponential on a wide diamond. Skipping `via` must leave both node sets identical, or
        freshness would compare a different closure than the sidecar recorded."""
        cases = [
            (self._seed_chain, "spec/component/top", "component/top@0.1.0"),
            (self._seed_diamond, "spec/problem/a", "problem/a@0.3.0"),
        ]
        for seed, spec_ref, node_key in cases:
            with self.subTest(seed=seed.__name__):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = Path(tmp)
                    _load_spec_catalog.cache_clear()
                    seed(repo)
                    kwargs = dict(target_spec_ref=spec_ref, target_node_key=node_key)
                    full, err_full = build_dependency_graph(repo, **kwargs)
                    _load_spec_catalog.cache_clear()
                    lite, err_lite = build_dependency_graph(
                        repo, include_via=False, **kwargs)
                    self.assertIsNone(err_full)
                    self.assertIsNone(err_lite)
                    self.assertEqual(full["all_nodes"], lite["all_nodes"])
                    self.assertEqual(full["node_key"], lite["node_key"])
                    # Membership is preserved (a set difference); only the paths are dropped.
                    self.assertEqual([d["node_key"] for d in full["transitive_deps"]],
                                     [d["node_key"] for d in lite["transitive_deps"]])
                    self.assertTrue(all(d["via"] == [] for d in lite["transitive_deps"]))
                    if seed is self._seed_diamond:
                        # Sanity: the full build really does have a `via` block to skip.
                        self.assertTrue(any(d["via"] for d in full["transitive_deps"]))

    def test_all_nodes_alone_does_not_identify_a_closure(self) -> None:
        """`topo_level` is a node's HEIGHT, so `a->b, a->c, b->c` and `a->b->c` share every
        `(node_key, topo_level)` pair. They differ only in whether `c` is direct or transitive.
        This is why `_closure_signature` compares the `transitive_deps` membership too — a
        deps.yaml edit that only moves an edge must still re-certify the node."""
        shapes = {}
        for label, a_deps in (("wide", ["b", "c"]), ("chain", ["b"])):
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                _load_spec_catalog.cache_clear()
                _write_catalog(repo, [
                    {"spec_kind": "component", "spec_id": s, "spec_version": "0.1.0",
                     "deps_path": f"spec/component/{s}/deps.yaml"} for s in ("a", "b", "c")])
                _write_deps(repo, "spec/component/a", "component", "a",
                            components=[(d, ">=0.1.0 <1.0.0") for d in a_deps])
                _write_deps(repo, "spec/component/b", "component", "b",
                            components=[("c", ">=0.1.0 <1.0.0")])
                _write_deps(repo, "spec/component/c", "component", "c")
                graph, err = build_dependency_graph(
                    repo, target_spec_ref="spec/component/a",
                    target_node_key="component/a@0.1.0")
                self.assertIsNone(err)
                shapes[label] = graph
        self.assertEqual(shapes["wide"]["all_nodes"], shapes["chain"]["all_nodes"])
        self.assertEqual([d["node_key"] for d in shapes["wide"]["transitive_deps"]], [])
        self.assertEqual([d["node_key"] for d in shapes["chain"]["transitive_deps"]],
                         ["component/c@0.1.0"])

    def test_missing_deps_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            graph, err = build_dependency_graph(
                repo, target_spec_ref="spec/component/nope",
                target_node_key="component/nope@0.1.0")
            self.assertIsNone(graph)
            self.assertEqual(err["reason"], "dependency_deps_unreadable")


if __name__ == "__main__":
    unittest.main()
