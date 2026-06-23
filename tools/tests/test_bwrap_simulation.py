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
    build_readonly_bwrap_profile,
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

    def test_hook_runtime_writes_land_in_scope(self) -> None:
        # The leaf's own PreToolUse/PostToolUse hooks (confined subprocesses) write runtime
        # bookkeeping outside write_roots: hook-event audit (hooks/) and the first-read
        # state (audit/<arid>.auto_reads_seen.json, which FAIL-CLOSES the hook if unwritable).
        # Both per-orchestration dirs must be writable inside the sandbox.
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t).resolve()
            orch, arid = "orch_hook", "arid_hook"
            self._setup(repo, orch, arid)
            _write_run_write_baseline(repo, orch, agent_run_id=arid)
            script = textwrap.dedent(f"""
                import os
                from pathlib import Path
                O = "workspace/orchestrations/{orch}"
                def report(tag, ok, e=""):
                    print(f"{{tag}}:{{'OK' if ok else 'FAIL'}}", e, flush=True)
                try:
                    p = Path(O + "/hooks/native_hook_events.jsonl")
                    with p.open("a", encoding="utf-8") as h:
                        h.write("{{}}\\n")
                    report("HOOK_AUDIT", True)
                except Exception as e:
                    report("HOOK_AUDIT", False, repr(e))
                try:
                    sp = O + "/audit/{arid}.auto_reads_seen.json"
                    fd = os.open(sp, os.O_RDWR | os.O_CREAT, 0o644)
                    os.write(fd, b"[]"); os.close(fd)
                    report("AUTO_READS_SEEN", True)
                except Exception as e:
                    report("AUTO_READS_SEEN", False, repr(e))
            """)
            profile = build_bwrap_profile(
                repo_root=repo, orchestration_id=orch, agent_run_id=arid,
                backend_command="python3", backend_type="claude")
            cmd = render_bwrap_command(
                profile=profile, command_argv=["python3", "-c", script])
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=90).stdout
            self.assertIn("HOOK_AUDIT:OK", out, out)
            self.assertIn("AUTO_READS_SEEN:OK", out, out)

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


@unittest.skipUnless(_bwrap_usable(), "bwrap / user namespaces not available")
class BwrapBuildPathSimulationTests(unittest.TestCase):
    """Model the Build phase's filesystem writes under bwrap (the highest-risk path per
    docs/BWRAP_ENABLEMENT.md). A Make/Fortran build routes outputs via OBJDIR/BINDIR
    overrides and side-outputs a cross-phase MCP command log into the (read-only) source
    tree. Confirm each lands inside -- or is correctly blocked outside -- the leaf's bwrap
    write scope before the billed E2E."""

    PIPE = "workspace/pipelines/n/p_001"
    BINARY_ROOT = PIPE + "/binary/"
    SRC_DIR = PIPE + "/source/src_001/src/"

    def _setup(self, repo: Path, orch: str, arid: str) -> None:
        _ensure_orchestration_audit_dirs(repo, orch)
        (repo / self.BINARY_ROOT).mkdir(parents=True, exist_ok=True)
        # The source tree is a READ input for Build (the code to compile).
        (repo / self.SRC_DIR).mkdir(parents=True, exist_ok=True)
        (repo / self.SRC_DIR / "foo_model.f90").write_text("module foo\nend module\n")
        cap_dir = _capabilities_dir(repo, orch); cap_dir.mkdir(parents=True, exist_ok=True)
        (cap_dir / f"{arid}.json").write_text(
            json.dumps({"agent_run_id": arid, "write_roots": [self.BINARY_ROOT]}),
            encoding="utf-8")
        rm_dir = _read_manifests_dir(repo, orch); rm_dir.mkdir(parents=True, exist_ok=True)
        (rm_dir / f"{arid}.json").write_text(
            json.dumps({"agent_run_id": arid,
                        "allowed_read_roots": [self.PIPE + "/source/"]}),
            encoding="utf-8")
        # The output manifest records the authorized cross-phase MCP log (a Make build
        # side-outputs it into the source tree); build_bwrap_profile reads it to make that
        # path writable inside the sandbox.
        from tools.orchestration_runtime import _write_allowed_output_manifest
        _write_allowed_output_manifest(
            repo, orchestration_id=orch, agent_run_id=arid,
            allowed_output_paths=[self.BINARY_ROOT + "bin_001/binary_meta.json"],
            allowed_file_tool_paths=[],
            allowed_tmp_root=f"workspace/tmp/{arid}",
            mcp_owned_audit_logs=[self.SRC_DIR + "mcp_command_log.jsonl"])

    def _leaf_script(self, arid: str) -> str:
        return textwrap.dedent(f"""
            import os
            from pathlib import Path
            PIPE = "{self.PIPE}"; ARID = "{arid}"
            def report(tag, ok, e=""):
                print(f"{{tag}}:{{'OK' if ok else 'FAIL'}}", e, flush=True)
            # OBJDIR override -> .o/.mod into the per-run tmp (workspace/tmp/<arid>/build)
            try:
                od = f"workspace/tmp/{{ARID}}/build"; os.makedirs(od, exist_ok=True)
                Path(od + "/foo.o").write_text("obj"); Path(od + "/foo.mod").write_text("mod")
                report("OBJDIR", True)
            except Exception as e:
                report("OBJDIR", False, repr(e))
            # BINDIR override -> the execution binary into the binary write_root
            try:
                bd = f"{{PIPE}}/binary/bin_001/bin"; os.makedirs(bd, exist_ok=True)
                Path(bd + "/foo_runner").write_text("ELF"); report("BINDIR", True)
            except Exception as e:
                report("BINDIR", False, repr(e))
            # binary_meta.json into the binary write_root
            try:
                Path(f"{{PIPE}}/binary/bin_001/binary_meta.json").write_text("{{}}")
                report("BINARY_META", True)
            except Exception as e:
                report("BINARY_META", False, repr(e))
            # cross-phase MCP command log -> source/<id>/src/ (outside write_roots, a read input)
            try:
                Path(f"{{PIPE}}/source/src_001/src/mcp_command_log.jsonl").write_text("{{}}\\n")
                report("XPHASE_MCP_LOG", True)
            except Exception as e:
                report("XPHASE_MCP_LOG", False, repr(e))
        """)

    def _run(self, repo: Path, orch: str, arid: str) -> str:
        profile = build_bwrap_profile(
            repo_root=repo, orchestration_id=orch, agent_run_id=arid,
            backend_command="python3", backend_type="claude")
        cmd = render_bwrap_command(
            profile=profile, command_argv=["python3", "-c", self._leaf_script(arid)])
        return subprocess.run(cmd, capture_output=True, text=True, timeout=90).stdout

    def test_build_outputs_land_in_write_scope(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t).resolve()
            orch, arid = "orch_build", "arid_build"
            self._setup(repo, orch, arid)
            _write_run_write_baseline(repo, orch, agent_run_id=arid)
            out = self._run(repo, orch, arid)
            # The make-or-break: object/.mod (OBJDIR->per-run tmp) and the exe + meta
            # (BINDIR/binary write_root) must all be writable inside the sandbox.
            self.assertIn("OBJDIR:OK", out, out)
            self.assertIn("BINDIR:OK", out, out)
            self.assertIn("BINARY_META:OK", out, out)

    def test_cross_phase_mcp_log_is_writable(self) -> None:
        # A Make/Fortran build side-outputs its MCP command log into source/<id>/src/,
        # which is outside the Build write_root (binary/ only) and is a read input. Under
        # Phase-2 the bwrap profile must still make that authorized cross-phase log path
        # writable, or compile_project fails with EROFS mid-build.
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t).resolve()
            orch, arid = "orch_build2", "arid_build2"
            self._setup(repo, orch, arid)
            _write_run_write_baseline(repo, orch, agent_run_id=arid)
            out = self._run(repo, orch, arid)
            self.assertIn("XPHASE_MCP_LOG:OK", out, out)


@unittest.skipUnless(_bwrap_usable(), "bwrap / user namespaces not available")
class BwrapReadonlyProfileTests(unittest.TestCase):
    """P2-4b: the failure diagnostician runs under a read-only bwrap profile
    (`build_readonly_bwrap_profile`) — no capability, no write_roots. Confirm the
    rendered sandbox lets the leaf READ the repo and write tmp scratch, but BLOCKS any
    repo write (no write_roots → repo stays ro), so a read-only reasoning leaf is
    confined with nothing to attribute (FS-diff trivially empty)."""

    def _leaf_script(self, arid: str) -> str:
        return textwrap.dedent(f"""
            import socket
            from pathlib import Path
            def report(tag, ok, e=""):
                print(f"{{tag}}:{{'OK' if ok else 'FAIL'}}", e, flush=True)
            # repo is ro-bound -> reading a repo file works
            try:
                Path("AGENTS_SIM.md").read_text(); report("READ_REPO", True)
            except Exception as e:
                report("READ_REPO", False, repr(e))
            # tmp scratch (workspace/tmp/<arid>) is bound rw
            try:
                Path("workspace/tmp/{arid}/scratch.txt").write_text("x"); report("WRITE_TMP", True)
            except Exception as e:
                report("WRITE_TMP", False, repr(e))
            # a repo write must be blocked: no write_roots, repo stays read-only
            try:
                Path("AGENTS_SIM.md").write_text("mutated")
                print("WRITE_REPO:ALLOWED", flush=True)   # bad: confinement failed
            except Exception:
                print("WRITE_REPO:BLOCKED", flush=True)   # good
            try:
                socket.gethostbyname("api.anthropic.com"); report("DNS", True)
            except Exception as e:
                report("DNS", False, repr(e))
        """)

    def test_readonly_profile_reads_repo_blocks_repo_write(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t).resolve()
            orch, arid = "orch_ro", "arid_ro"
            _ensure_orchestration_audit_dirs(repo, orch)
            (repo / "AGENTS_SIM.md").write_text("orig\n", encoding="utf-8")
            profile = build_readonly_bwrap_profile(
                repo_root=repo, orchestration_id=orch, agent_run_id=arid,
                backend_command="python3", backend_type="claude")
            self.assertTrue(profile.get("readonly"))
            self.assertEqual(profile.get("write_roots"), [])
            cmd = render_bwrap_command(
                profile=profile, command_argv=["python3", "-c", self._leaf_script(arid)])
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=90).stdout
            self.assertIn("READ_REPO:OK", out, out)
            self.assertIn("WRITE_TMP:OK", out, out)
            self.assertIn("WRITE_REPO:BLOCKED", out, out)
            self.assertIn("DNS:OK", out, out)
            # the repo file is unchanged on the host
            self.assertEqual((repo / "AGENTS_SIM.md").read_text(), "orig\n")


if __name__ == "__main__":
    unittest.main()
