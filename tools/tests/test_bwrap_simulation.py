"""Layer-1 bwrap simulation (Phase-2 redesign).

Fast (seconds, no LLM, no API) reproduction of the leaf's filesystem behavior under a real
rendered bwrap profile. A synthetic "leaf" script performs the operations a real leaf does
(write new artifacts, rewrite an existing read-input file, atomic write, read inputs, attempt
an out-of-scope write, DNS), and we assert confinement + that the existing FS-diff machinery
(`_actual_changed_paths_since_baseline`) attributes exactly the in-scope writes.

This encodes the Phase-2 target behavior and currently FAILS against the file-pin model
(`REWRITE_READINPUT` hits EBUSY/EROFS because the read-input file is ro-pinned while it must
also be writable). It turns green once write/read binds become directory-level (§1) and
authorization is FS-diff containment (§2).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.orchestration_runtime import (
    _capabilities_dir,
    _read_manifests_dir,
    _ensure_orchestration_audit_dirs,
    _write_run_write_baseline,
    _actual_changed_paths_since_baseline,
    build_bwrap_profile,
    render_bwrap_command,
)

# The synthetic leaf. Runs INSIDE the sandbox (cwd = repo_root). Prints one TAG:RESULT per
# operation so the test can assert each independently.
_LEAF_SCRIPT = textwrap.dedent(
    """
    import os, socket
    from pathlib import Path
    IR = "workspace/ir/n/p_001"

    def report(tag, ok):
        print(f"{tag}:{'OK' if ok else 'FAIL'}", flush=True)

    # (a) write a brand-new artifact into the write_root dir
    try:
        Path(IR + "/ir_meta.json").write_text('{"x":1}'); report("WRITE_NEW", True)
    except Exception as e:
        print("WRITE_NEW:FAIL", repr(e), flush=True)

    # (c) atomic write (tmp + os.replace) of a new artifact in the write_root dir
    try:
        t = IR + "/.atomic.tmp"; Path(t).write_text("a"); os.replace(t, IR + "/atomic.json")
        report("ATOMIC_NEW", True)
    except Exception as e:
        print("ATOMIC_NEW:FAIL", repr(e), flush=True)

    # (b/d) rewrite the read-INPUT file (spec.ir.yaml) via atomic rename -- the EBUSY case.
    # spec.ir.yaml is both a read input AND written (compile.verify appends io_contract).
    try:
        t = IR + "/.spec.tmp"; Path(t).write_text("meta: {}\\nio_contract: {}\\n")
        os.replace(t, IR + "/spec.ir.yaml"); report("REWRITE_READINPUT", True)
    except Exception as e:
        print("REWRITE_READINPUT:FAIL", repr(e), flush=True)

    # (e) read the read input
    try:
        Path(IR + "/spec.ir.yaml").read_text(); report("READ_INPUT", True)
    except Exception as e:
        print("READ_INPUT:FAIL", repr(e), flush=True)

    # (f) attempt a write OUTSIDE the write scope (repo file) -- must be blocked.
    try:
        Path("AGENTS_SIM.md").write_text("x")
        print("WRITE_OUTSIDE:ALLOWED", flush=True)   # bad: confinement failed
    except Exception:
        print("WRITE_OUTSIDE:BLOCKED", flush=True)   # good

    # (g) DNS reachability (the resolv.conf symlink fix)
    try:
        socket.gethostbyname("api.anthropic.com"); report("DNS", True)
    except Exception as e:
        print("DNS:FAIL", repr(e), flush=True)
    """
)


def _bwrap_usable() -> bool:
    if shutil.which("bwrap") is None:
        return False
    binds: list[str] = []
    for p in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(p).exists():
            binds += ["--ro-bind", p, p]
    try:
        r = subprocess.run(
            ["bwrap", *binds, "--dev", "/dev", "--", "/bin/true"],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


@unittest.skipUnless(_bwrap_usable(), "bwrap / user namespaces not available")
class BwrapSimulationTests(unittest.TestCase):
    IR_DIR = "workspace/ir/n/p_001/"
    SPEC = IR_DIR + "spec.ir.yaml"

    def _setup(self, repo: Path, orch: str, arid: str) -> None:
        _ensure_orchestration_audit_dirs(repo, orch)
        (repo / self.IR_DIR).mkdir(parents=True, exist_ok=True)
        (repo / self.SPEC).write_text("meta: {}\n", encoding="utf-8")
        (repo / "AGENTS_SIM.md").write_text("orig\n", encoding="utf-8")
        cap_dir = _capabilities_dir(repo, orch); cap_dir.mkdir(parents=True, exist_ok=True)
        (cap_dir / f"{arid}.json").write_text(
            json.dumps({"agent_run_id": arid, "write_roots": [self.IR_DIR]}), encoding="utf-8")
        rm_dir = _read_manifests_dir(repo, orch); rm_dir.mkdir(parents=True, exist_ok=True)
        # spec.ir.yaml as a FILE read root -> reproduces the ro file-pin that conflicts with
        # the writable write_root dir it lives in.
        (rm_dir / f"{arid}.json").write_text(
            json.dumps({"agent_run_id": arid, "allowed_read_roots": [self.SPEC]}),
            encoding="utf-8")

    def _run_leaf(self, repo: Path, orch: str, arid: str) -> str:
        profile = build_bwrap_profile(
            repo_root=repo, orchestration_id=orch, agent_run_id=arid,
            backend_command="python3", backend_type="claude")
        cmd = render_bwrap_command(profile=profile, command_argv=["python3", "-c", _LEAF_SCRIPT])
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        return res.stdout

    def test_confinement_and_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t).resolve()
            orch, arid = "orch_sim", "arid_sim"
            self._setup(repo, orch, arid)
            _write_run_write_baseline(repo, orch, agent_run_id=arid)
            out = self._run_leaf(repo, orch, arid)

            self.assertIn("WRITE_NEW:OK", out, out)
            self.assertIn("ATOMIC_NEW:OK", out, out)
            self.assertIn("READ_INPUT:OK", out, out)
            self.assertIn("WRITE_OUTSIDE:BLOCKED", out, out)
            self.assertIn("DNS:OK", out, out)
            # The Phase-2 target: a read-input file inside a write_root must be rewritable.
            # FAILS against the current file-pin model (EBUSY/EROFS); passes after §1.
            self.assertIn("REWRITE_READINPUT:OK", out, out)
            # out-of-scope file must be unchanged on the host
            self.assertEqual((repo / "AGENTS_SIM.md").read_text(), "orig\n")

    def test_fs_diff_attributes_in_scope_writes_only(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t).resolve()
            orch, arid = "orch_sim2", "arid_sim2"
            self._setup(repo, orch, arid)
            _write_run_write_baseline(repo, orch, agent_run_id=arid)
            self._run_leaf(repo, orch, arid)
            changed = set(_actual_changed_paths_since_baseline(repo, orch, agent_run_id=arid))
            # in-scope artifacts attributed
            self.assertIn(self.IR_DIR + "ir_meta.json", changed, changed)
            self.assertIn(self.IR_DIR + "atomic.json", changed, changed)
            # out-of-scope file never attributed (it was never written)
            self.assertNotIn("AGENTS_SIM.md", changed, changed)
            # every attributed path is inside the declared write_root dir
            for p in changed:
                self.assertTrue(
                    p.startswith(self.IR_DIR), f"{p} escaped write_root {self.IR_DIR}")


if __name__ == "__main__":
    unittest.main()
