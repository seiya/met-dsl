"""Tests for mcp_servers/build_runtime_server.py.

Bytecode-cache handling: the build-runtime MCP server runs inside a read-only bwrap
sandbox. It must never attempt to write Python bytecode (the previous code
unconditionally created `workspace/.pycache`, which EROFSed before any build ran on a
clean workspace).

run_syntax_check: the Generate.syntax compiler front-end gate — adapter argv shape,
module/use topological source ordering, missing-compiler skip, custom-command
rejection, and (when gfortran is installed) a real -fsyntax-only smoke covering the
error classes the retired post_generate text heuristics used to mimic.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SERVER_PATH = (
    Path(__file__).resolve().parent.parent.parent / "mcp_servers" / "build_runtime_server.py"
)


def _load_server_module():
    spec = importlib.util.spec_from_file_location("build_runtime_server", _SERVER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so module-level @dataclass can resolve its __module__.
    sys.modules["build_runtime_server"] = mod
    spec.loader.exec_module(mod)
    return mod


class DisableBytecodeWritesTests(unittest.TestCase):
    def test_disable_sets_interpreter_flag_and_env(self) -> None:
        mod = _load_server_module()
        orig_flag = sys.dont_write_bytecode
        orig_env = os.environ.get("PYTHONDONTWRITEBYTECODE")
        try:
            sys.dont_write_bytecode = False
            os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
            mod._disable_bytecode_writes()
            # The interpreter flag must flip (a runtime env var alone is too late) so
            # importlib does not write .pyc; the env var is exported for subprocesses.
            self.assertTrue(sys.dont_write_bytecode)
            self.assertEqual(os.environ.get("PYTHONDONTWRITEBYTECODE"), "1")
        finally:
            sys.dont_write_bytecode = orig_flag
            if orig_env is None:
                os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
            else:
                os.environ["PYTHONDONTWRITEBYTECODE"] = orig_env

    def test_runtime_loader_does_not_mkdir_pycache(self) -> None:
        # Regression: the server must not create workspace/.pycache (read-only under the
        # bwrap sandbox -> EROFS before any build runs).
        src = _SERVER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("pycache_root.mkdir", src)


_HAVE_GFORTRAN = shutil.which("gfortran") is not None


class RunSyntaxCheckTests(unittest.TestCase):
    """Unit tests for tool_run_syntax_check (no compiler required — subprocess mocked
    or skipped paths)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def _src_dir(self, files: dict[str, str]) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for name, text in files.items():
            (d / name).write_text(text, encoding="utf-8")
        return d

    def test_gfortran_adapter_argv_shape(self) -> None:
        argv = self.mod._gfortran_syntax_argv("f2008", ".mods", False, ["a.f90", "b.f90"])
        self.assertEqual(
            argv,
            ["gfortran", "-fsyntax-only", "-std=f2008",
             "-Werror=unused-dummy-argument", "-Werror=unused-variable",
             "-J", ".mods", "-I", ".mods",
             "a.f90", "b.f90"])
        argv = self.mod._gfortran_syntax_argv("f2018", ".mods", True, ["x.f90"])
        self.assertIn("-fopenmp", argv)
        self.assertIn("-std=f2018", argv)
        self.assertIn("-Werror=unused-dummy-argument", argv)
        self.assertIn("-Werror=unused-variable", argv)
        # sources stay last so the compiler reads them after the mod-dir flags
        self.assertEqual(argv[-1], "x.f90")

    def test_source_order_topological_by_module_use(self) -> None:
        d = self._src_dir({
            # alphabetically first but uses the module defined last
            "a_runner.f90": "program p\n  use z_model, only: x\nend program p\n",
            "m_checks.f90": "module m_checks\n  use z_model\nend module m_checks\n",
            "z_model.f90": "module z_model\n  integer :: x\nend module z_model\n",
        })
        self.assertEqual(
            self.mod._fortran_syntax_source_order(d),
            ["z_model.f90", "a_runner.f90", "m_checks.f90"])

    def test_source_order_ignores_identifier_starting_with_use(self) -> None:
        # `use\b` guards against an ordinary identifier that merely starts with "use"
        # (user_flag / usedcount) being parsed as a USE statement and minting a bogus edge.
        d = self._src_dir({
            "a.f90": "program p\n  logical :: user_flag\n  integer :: usedcount\n"
                     "  user_flag = .true.\n  usedcount = 2\nend program p\n",
            "user.f90": "module user\nend module user\n",  # would be a false provider
        })
        # user.f90 defines module `user`; if `user_flag` were mis-parsed as `use r_flag`/
        # `use user`, ordering could shuffle. With the fix a.f90 has no real `use`, so the
        # order is a plain name-sort and no spurious dependency is introduced.
        self.assertEqual(
            self.mod._fortran_syntax_source_order(d), ["a.f90", "user.f90"])

    def test_source_order_ignores_unknown_and_intrinsic_modules(self) -> None:
        d = self._src_dir({
            "a.f90": "program p\n  use, intrinsic :: iso_fortran_env, only: int64\n"
                     "  use some_external_lib\nend program p\n",
        })
        self.assertEqual(self.mod._fortran_syntax_source_order(d), ["a.f90"])

    def test_source_order_module_procedure_not_a_definition(self) -> None:
        d = self._src_dir({
            "a.f90": "submodule (m) impl\ncontains\nmodule procedure f\nend procedure f\n"
                     "end submodule impl\n",
            "b.f90": "module b_mod\nend module b_mod\n",
        })
        # `module procedure` must not register a module named "procedure"/f.
        self.assertEqual(self.mod._fortran_syntax_source_order(d), ["a.f90", "b.f90"])

    def test_rejects_custom_command(self) -> None:
        d = self._src_dir({})
        with self.assertRaises(ValueError):
            self.mod.tool_run_syntax_check(
                {"project_dir": str(d), "command": ["gfortran", "x.f90"]})

    def test_rejects_unknown_compiler(self) -> None:
        d = self._src_dir({})
        with self.assertRaises(ValueError) as ctx:
            self.mod.tool_run_syntax_check({"project_dir": str(d), "compiler": "frt"})
        self.assertIn("supported=gfortran", str(ctx.exception))

    def test_missing_compiler_returns_skipped(self) -> None:
        d = self._src_dir({"a.f90": "program p\nend program p\n"})
        with mock.patch.object(self.mod.shutil, "which", return_value=None):
            result = self.mod.tool_run_syntax_check({"project_dir": str(d)})
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertIn("compiler not available", result["reason"])

    def test_no_sources_returns_skipped(self) -> None:
        d = self._src_dir({"notes.txt": "not fortran"})
        with mock.patch.object(self.mod.shutil, "which", return_value="/usr/bin/gfortran"):
            result = self.mod.tool_run_syntax_check({"project_dir": str(d)})
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertIn("no fortran sources", result["reason"])

    def test_run_invokes_adapter_argv_and_logs(self) -> None:
        d = self._src_dir({
            "m.f90": "module m\nend module m\n",
            "p.f90": "program p\n  use m\nend program p\n",
        })
        fake = subprocess.CompletedProcess(
            args=["gfortran"], returncode=0, stdout="", stderr="")
        with mock.patch.object(self.mod.shutil, "which", return_value="/usr/bin/gfortran"), \
                mock.patch.object(self.mod.subprocess, "run", return_value=fake) as run_mock:
            result = self.mod.tool_run_syntax_check(
                {"project_dir": str(d), "std": "f2008", "openmp": True})
        # first call = the syntax check itself; a later call probes --version
        argv = run_mock.call_args_list[0].args[0]
        self.assertEqual(argv[:3], ["gfortran", "-fsyntax-only", "-std=f2008"])
        self.assertIn("-fopenmp", argv)
        self.assertIn("-Werror=unused-dummy-argument", argv)
        self.assertIn("-Werror=unused-variable", argv)
        self.assertEqual(argv[-2:], ["m.f90", "p.f90"])  # topological order
        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["compiler"], "gfortran")
        # command_log.jsonl record with the run_syntax_check tool_name
        log_path = d / "command_log.jsonl"
        self.assertTrue(log_path.exists())
        entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["tool_name"], "run_syntax_check")
        self.assertEqual(entry["ok"], True)
        # scratch mod dir is created inside project_dir, isolated per call
        self.assertTrue((d / ".mods").is_dir())

    def test_compile_error_returns_ok_false(self) -> None:
        d = self._src_dir({"bad.f90": "program p\n  implicit none (external)\nend program p\n"})
        fake = subprocess.CompletedProcess(
            args=["gfortran"], returncode=1, stdout="",
            stderr="Error: Fortran 2018: IMPLICIT NONE with spec list")
        with mock.patch.object(self.mod.shutil, "which", return_value="/usr/bin/gfortran"), \
                mock.patch.object(self.mod.subprocess, "run", return_value=fake):
            result = self.mod.tool_run_syntax_check({"project_dir": str(d)})
        self.assertFalse(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertIn("IMPLICIT NONE with spec list", result["stderr"])


@unittest.skipUnless(_HAVE_GFORTRAN, "gfortran not available")
class RunSyntaxCheckGfortranSmokeTests(unittest.TestCase):
    """Real-compiler smoke: the gate must catch, with the actual gfortran front-end,
    the error classes the retired post_generate text heuristics used to mimic
    (identifier > 63 chars / implicit none spec-list / non-constant STOP code) plus the
    two promoted warning classes (unused dummy argument / unused variable), and must pass
    a valid two-file module dependency (define-before-use via .mod written by
    -fsyntax-only) as well as the associate binding that sanctions an inert dummy."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_server_module()

    def _check(self, files: dict[str, str]) -> dict:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for name, text in files.items():
            (d / name).write_text(text, encoding="utf-8")
        return self.mod.tool_run_syntax_check({"project_dir": str(d), "std": "f2008"})

    def test_valid_module_dependency_passes(self) -> None:
        result = self._check({
            "dep_model.f90": "module dep_model\n  implicit none\n  integer :: n = 1\n"
                             "end module dep_model\n",
            "top_runner.f90": "program top_runner\n  use dep_model, only: n\n"
                              "  implicit none\n  print *, n\nend program top_runner\n",
        })
        self.assertTrue(result["ok"], msg=result.get("stderr"))

    def test_implicit_none_spec_list_fails_under_f2008(self) -> None:
        result = self._check({
            "bad.f90": "program bad\n  implicit none (external)\nend program bad\n",
        })
        self.assertFalse(result["ok"])

    def test_over_63_char_identifier_fails(self) -> None:
        long_name = "x" * 64
        result = self._check({
            "bad.f90": f"program bad\n  implicit none\n  integer :: {long_name}\n"
                       f"  {long_name} = 1\nend program bad\n",
        })
        self.assertFalse(result["ok"])

    def test_nonconstant_stop_code_fails_under_f2008(self) -> None:
        result = self._check({
            "bad.f90": "program bad\n  implicit none\n"
                       "  character(len=8) :: cid\n  cid = 'c1'\n"
                       "  error stop 'unknown case_id: '//cid\nend program bad\n",
        })
        self.assertFalse(result["ok"])

    def test_unused_dummy_argument_fails(self) -> None:
        # A dummy the interface fixes but the body never reads is a dead dummy: the gate
        # must reject it so the leaf binds it with the associate idiom instead.
        result = self._check({
            "m.f90": "module m\n  implicit none\ncontains\n"
                     "  subroutine step(z_b, y)\n"
                     "    real, intent(in) :: z_b\n    real, intent(out) :: y\n"
                     "    y = 1.0\n  end subroutine step\n"
                     "end module m\n",
        })
        self.assertFalse(result["ok"])
        self.assertIn("unused-dummy-argument", result["stderr"])

    def test_unused_variable_fails(self) -> None:
        result = self._check({
            "bad.f90": "program bad\n  implicit none\n  integer :: leftover\n"
                       "  print *, 1\nend program bad\n",
        })
        self.assertFalse(result["ok"])
        self.assertIn("unused-variable", result["stderr"])

    def test_canary_source_is_valid_under_every_standard_and_detects_a_bad_std(self) -> None:
        # The conductor compiles SYNTAX_CANARY_SOURCE with the failing stage's own argv to
        # tell a broken INVOCATION (an `-std=` value the driver rejects, so no source is ever
        # parsed) apart from broken sources. Both halves of that must hold against the real
        # compiler: the canary passes under each standard a node may target — were it invalid
        # Fortran, EVERY failing stage would be misattributed to an unviable invocation and
        # nothing would ever reach the leaf — and it fails when the std is not one the driver
        # knows, which is the signal the attribution keys on.
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "metdsl_syntax_canary.f90").write_text(
            self.mod.SYNTAX_CANARY_SOURCE, encoding="utf-8")
        # every standard a node may declare — a canary that failed any one of these would
        # fail_closed every ordinary syntax finding on a node targeting it
        for std in ("f95", "f2003", "f2008", "f2018", "gnu", "legacy"):
            result = self.mod.tool_run_syntax_check({"project_dir": str(d), "std": std})
            self.assertTrue(result["ok"], msg=f"{std}: {result.get('stderr')}")
        bad = self.mod.tool_run_syntax_check({"project_dir": str(d), "std": "2008"})
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["skipped"])

    def test_default_on_warning_names_its_file_without_failing_the_gate(self) -> None:
        # Only the two promoted classes are errors. Other default-on warnings (-Wampersand
        # here) still print, anchored to their file, on a source the gate PASSES. The
        # conductor's dependency attribution (`_gate_syntax_check`) relies on exactly this: a
        # staged dependency's filename appearing in a failing stage's output proves nothing
        # about whose defect it is, so attribution asks the compiler (does the dependency
        # closure pass on its own?) instead of reading the diagnostics.
        result = self._check({
            "noisy.f90": "module noisy\n  implicit none\ncontains\n"
                         "  subroutine msg(u)\n    integer, intent(in) :: u\n"
                         "    write (u, '(a)') 'a message that is &\n"
                         "      continued'\n"
                         "  end subroutine msg\nend module noisy\n",
        })
        self.assertTrue(result["ok"], msg=result.get("stderr"))
        self.assertIn("noisy.f90", result["stderr"])
        self.assertIn("Wampersand", result["stderr"])

    def test_associate_binding_suppresses_unused_dummy(self) -> None:
        # Pins the sanctioned escape hatch: the very idiom CHECKS_MODULE_CONTRACT §5
        # mandates must pass this gate, so gate and doc cannot drift apart.
        result = self._check({
            "m.f90": "module m\n  implicit none\ncontains\n"
                     "  subroutine step(z_b, y)\n"
                     "    real, intent(in) :: z_b\n    real, intent(out) :: y\n"
                     "    associate (unused_z_b => z_b)\n    end associate\n"
                     "    y = 1.0\n  end subroutine step\n"
                     "end module m\n",
        })
        self.assertTrue(result["ok"], msg=result.get("stderr"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
