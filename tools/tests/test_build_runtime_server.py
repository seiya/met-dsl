"""Tests for mcp_servers/build_runtime_server.py bytecode-cache handling.

The build-runtime MCP server runs inside a read-only bwrap sandbox. It must never
attempt to write Python bytecode (the previous code unconditionally created
`workspace/.pycache`, which EROFSed before any build ran on a clean workspace).
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
