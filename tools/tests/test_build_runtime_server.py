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
            ["gfortran", "-fsyntax-only", "-std=f2008", "-J", ".mods", "-I", ".mods",
             "a.f90", "b.f90"])
        argv = self.mod._gfortran_syntax_argv("f2018", ".mods", True, ["x.f90"])
        self.assertIn("-fopenmp", argv)
        self.assertIn("-std=f2018", argv)
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
    (identifier > 63 chars / implicit none spec-list / non-constant STOP code), and
    must pass a valid two-file module dependency (define-before-use via .mod written
    by -fsyntax-only)."""

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
