#!/usr/bin/env python3
"""Unit tests for tools/runner_renderer (R1/M3c-β host-rendered runner glue).

`render_runner` is a pure function of the IR: these tests pin its rendered shape
(a boundary-copy IR and a metrics-bearing IR), determinism, the render-error
matrix, and the harness signature pin. A `gfortran`-gated smoke compiles+runs the
rendered runner against a v2 harness stub + a fixed-ABI checks stub end-to-end.
"""

from __future__ import annotations

import copy
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.runner_renderer import (
    EXPECTED_HARNESS_SPEC_ID,
    RenderError,
    _HARNESS_V2_INTERFACE,
    assert_harness_pin,
    render_runner,
)
from tools.validate_pipeline_semantics import _parse_interface_stanzas

HARNESS = "harness_fortran_cpu"
BOUNDARY_SID = "dynamics_shallow_water_boundary_2d_periodic_copy"

_HAVE_GFORTRAN = shutil.which("gfortran") is not None


def _boundary_ir() -> dict:
    """A boundary_2d_periodic_copy-shaped IR: 3 cases (2 pass + 1 xfail), rank-2 +
    scalar snapshot variables, 3 checks, no metrics, 1 infra dep."""
    return {
        "meta": {"spec_id": BOUNDARY_SID, "spec_kind": "component"},
        "case": {"test_case_set": [
            {"case_id": "l0_periodic_x_wrap_pass"},
            {"case_id": "l0_periodic_y_wrap_pass"},
            {"case_id": "l0_invalid_ny_xfail"},
        ]},
        "impl_defaults": {
            "target": {"class": "cpu", "backend": "openmp"},
            "toolchain": {"language": "fortran", "standard": "f2008", "build_system": "make"},
            "backend_overrides": {"openmp": {"num_threads": 1}},
        },
        "io_contract": {
            "raw_requirements": {"required_evidence": [
                {"artifact": "state_snapshots", "required": True, "min_samples": 3, "schema": {
                    "variables": [
                        {"name": "field_ghost", "shape_expr": "[4, 4]"},
                        {"name": "field_interior", "shape_expr": "[2, 2]"},
                        {"name": "max_abs_deviation", "shape_expr": "scalar"},
                        {"name": "guard_fired", "shape_expr": "scalar"},
                    ],
                    "time_variable": "t", "time_shape_expr": "scalar",
                }},
                {"artifact": "metrics_basis.json", "required": True, "min_samples": 1},
            ]},
            "test_evidence_requirements": [
                {"test_id": "l0_periodic_x_wrap_pass",
                 "required_raw_variables": ["field_ghost", "field_interior", "max_abs_deviation"]},
                {"test_id": "l0_periodic_y_wrap_pass",
                 "required_raw_variables": ["field_ghost", "field_interior", "max_abs_deviation"]},
                {"test_id": "l0_invalid_ny_xfail",
                 "required_raw_variables": ["guard_fired"]},
            ],
            "diagnostics_contract": {
                "checks": [{"id": "x_wrap"}, {"id": "y_wrap"}, {"id": "input_guard"}],
                "verdict": {"required": True, "fields": ["overall", "failed_checks"]},
            },
            "test_predicates": [
                {"test_id": "l0_periodic_x_wrap_pass", "expected_outcome": "pass",
                 "target_cases": ["l0_periodic_x_wrap_pass"]},
                {"test_id": "l0_periodic_y_wrap_pass", "expected_outcome": "pass",
                 "target_cases": ["l0_periodic_y_wrap_pass"]},
                {"test_id": "l0_invalid_ny_xfail", "expected_outcome": "xfail",
                 "target_cases": ["l0_invalid_ny_xfail"]},
            ],
        },
        "dependency": {
            "node_key": f"component/{BOUNDARY_SID}@0.1.0",
            "direct_deps": [{"node_key": "infrastructure/harness_fortran_cpu@0.2.0"}],
        },
    }


def _metrics_ir() -> dict:
    """A minimal problem-shaped IR with a metric address, to pin the metric_compute
    rendering path."""
    return {
        "meta": {"spec_id": "prob_x", "spec_kind": "problem"},
        "case": {"test_case_set": [{"case_id": "c_pass"}]},
        "impl_defaults": {
            "target": {"class": "cpu", "backend": "openmp"},
            "backend_overrides": {"openmp": {"num_threads": 4}},
        },
        "io_contract": {
            "raw_requirements": {"required_evidence": [
                {"artifact": "state_snapshots", "schema": {
                    "variables": [{"name": "u", "shape_expr": "[3]"}],
                    "time_variable": "t",
                }},
            ]},
            "test_evidence_requirements": [
                {"test_id": "c_pass", "required_raw_variables": ["u"]},
            ],
            "diagnostics_contract": {
                "checks": [{"id": "conv"}],
                "metrics": ["error.l2", "error.linf"],
                "verdict": {"fields": ["overall", "failed_checks"]},
            },
            "test_predicates": [
                {"test_id": "c_pass", "expected_outcome": "pass", "target_cases": ["c_pass"]},
            ],
        },
        "dependency": {"node_key": "problem/prob_x@0.1.0",
                       "direct_deps": [{"node_key": "infrastructure/harness_fortran_cpu@0.2.0"}]},
    }


def _harness_signatures() -> list[dict]:
    """The certified harness IR public_api.signatures, synthesized from §5.1."""
    ops, types, errs = _parse_interface_stanzas(_HARNESS_V2_INTERFACE)
    assert not errs, errs
    return [{"symbol": n, "interface": "\n".join(lines)}
            for n, lines in {**ops, **types}.items()]


# The v2 harness stub source (canonical `type ::` + separate `public ::`), used by
# the pin test and the gfortran smoke. Only enough body to link + emit outputs.
_HARNESS_STUB = textwrap.dedent("""\
    module harness_fortran_cpu_model
      use, intrinsic :: iso_fortran_env, only: real64
      ! allow(C003)
      implicit none
      private
      integer, parameter :: dp = real64
      integer, parameter :: case_id_len = 64
      type :: harness_fortran_cpu__h_named
        character(len=:), allocatable :: name
        character(len=:), allocatable :: json
      end type harness_fortran_cpu__h_named
      type :: harness_fortran_cpu__h_check
        character(len=:), allocatable :: id
        character(len=4) :: status
      end type harness_fortran_cpu__h_check
      type :: harness_fortran_cpu__h_metric
        character(len=:), allocatable :: name
        real(dp) :: value
        logical :: is_na
        character(len=:), allocatable :: reason_na
      end type harness_fortran_cpu__h_metric
      type :: harness_fortran_cpu__h_case_result
        character(len=:), allocatable :: case_id
        logical :: expected_xfail
        type(harness_fortran_cpu__h_check), allocatable :: checks(:)
        type(harness_fortran_cpu__h_metric), allocatable :: metrics(:)
      end type harness_fortran_cpu__h_case_result
      type :: harness_fortran_cpu__h_mb_entry
        character(len=:), allocatable :: test_id
        type(harness_fortran_cpu__h_named), allocatable :: values(:)
      end type harness_fortran_cpu__h_mb_entry
      public :: harness_fortran_cpu__h_named, harness_fortran_cpu__h_check
      public :: harness_fortran_cpu__h_metric, harness_fortran_cpu__h_case_result
      public :: harness_fortran_cpu__h_mb_entry
      public :: harness_fortran_cpu__parse_cases, harness_fortran_cpu__emit_real
      public :: harness_fortran_cpu__emit_int, harness_fortran_cpu__emit_bool
      public :: harness_fortran_cpu__emit_array_r1, harness_fortran_cpu__emit_array_r2
      public :: harness_fortran_cpu__emit_array_r3, harness_fortran_cpu__emit_array_r4
      public :: harness_fortran_cpu__box, harness_fortran_cpu__write_snapshot
      public :: harness_fortran_cpu__write_metrics_basis
      public :: harness_fortran_cpu__write_diagnostics, harness_fortran_cpu__write_perf
    contains
      subroutine harness_fortran_cpu__parse_cases(tokens, ntokens, case_ids, ncases, ok)
        character(len=*), intent(in) :: tokens(:)
        integer, intent(in) :: ntokens
        character(len=case_id_len), intent(out) :: case_ids(:)
        integer, intent(out) :: ncases
        logical, intent(out) :: ok
        integer :: i, pos
        ncases = 0
        ok = .false.
        pos = 0
        do i = 1, ntokens
          if (trim(tokens(i)) == '--cases') pos = i
        end do
        if (pos == 0 .or. pos + 1 > ntokens) return
        do i = pos + 2, ntokens
          ncases = ncases + 1
          case_ids(ncases) = tokens(i)
        end do
        ok = ncases > 0
      end subroutine harness_fortran_cpu__parse_cases
      function harness_fortran_cpu__emit_real(x) result(s)
        real(dp), intent(in) :: x
        character(len=:), allocatable :: s
        character(len=32) :: buf
        write(buf, '(ES24.16E3)') x
        s = trim(adjustl(buf))
      end function harness_fortran_cpu__emit_real
      function harness_fortran_cpu__emit_int(i) result(s)
        integer, intent(in) :: i
        character(len=:), allocatable :: s
        character(len=32) :: buf
        write(buf, '(I0)') i
        s = trim(adjustl(buf))
      end function harness_fortran_cpu__emit_int
      function harness_fortran_cpu__emit_bool(b) result(s)
        logical, intent(in) :: b
        character(len=:), allocatable :: s
        if (b) then
          s = 'true'
        else
          s = 'false'
        end if
      end function harness_fortran_cpu__emit_bool
      function harness_fortran_cpu__emit_array_r1(a) result(s)
        real(dp), intent(in) :: a(:)
        character(len=:), allocatable :: s
        integer :: i
        s = '['
        do i = 1, size(a)
          if (i > 1) s = s // ', '
          s = s // harness_fortran_cpu__emit_real(a(i))
        end do
        s = s // ']'
      end function harness_fortran_cpu__emit_array_r1
      function harness_fortran_cpu__emit_array_r2(a) result(s)
        real(dp), intent(in) :: a(:,:)
        character(len=:), allocatable :: s
        integer :: i
        s = '['
        do i = 1, size(a, 1)
          if (i > 1) s = s // ', '
          s = s // harness_fortran_cpu__emit_array_r1(a(i, :))
        end do
        s = s // ']'
      end function harness_fortran_cpu__emit_array_r2
      function harness_fortran_cpu__emit_array_r3(a) result(s)
        real(dp), intent(in) :: a(:,:,:)
        character(len=:), allocatable :: s
        integer :: i
        s = '['
        do i = 1, size(a, 1)
          if (i > 1) s = s // ', '
          s = s // harness_fortran_cpu__emit_array_r2(a(i, :, :))
        end do
        s = s // ']'
      end function harness_fortran_cpu__emit_array_r3
      function harness_fortran_cpu__emit_array_r4(a) result(s)
        real(dp), intent(in) :: a(:,:,:,:)
        character(len=:), allocatable :: s
        integer :: i
        s = '['
        do i = 1, size(a, 1)
          if (i > 1) s = s // ', '
          s = s // harness_fortran_cpu__emit_array_r3(a(i, :, :, :))
        end do
        s = s // ']'
      end function harness_fortran_cpu__emit_array_r4
      function harness_fortran_cpu__box(name, json) result(nv)
        character(len=*), intent(in) :: name
        character(len=*), intent(in) :: json
        type(harness_fortran_cpu__h_named) :: nv
        nv%name = name
        nv%json = json
      end function harness_fortran_cpu__box
      subroutine harness_fortran_cpu__write_snapshot(case_id, values, time)
        character(len=*), intent(in) :: case_id
        type(harness_fortran_cpu__h_named), intent(in) :: values(:)
        real(dp), intent(in) :: time
        integer :: u, k
        open(newunit=u, file='raw/state_snapshots/'//trim(case_id)//'.json', status='replace')
        write(u, '(A)') '{'
        do k = 1, size(values)
          write(u, '(A)') '  "'//trim(values(k)%name)//'": '//values(k)%json//','
        end do
        write(u, '(A)') '  "t": '//harness_fortran_cpu__emit_real(time)
        write(u, '(A)') '}'
        close(u)
      end subroutine harness_fortran_cpu__write_snapshot
      subroutine harness_fortran_cpu__write_metrics_basis(entries, n)
        type(harness_fortran_cpu__h_mb_entry), intent(in) :: entries(:)
        integer, intent(in) :: n
        integer :: u, k, j
        open(newunit=u, file='raw/metrics_basis.json', status='replace')
        write(u, '(A)') '{ "per_test": ['
        do k = 1, n
          write(u, '(A)') '  { "test_id": "'//trim(entries(k)%test_id)//'"'
          do j = 1, size(entries(k)%values)
            write(u, '(A)') '  , "'//trim(entries(k)%values(j)%name)//'": '//entries(k)%values(j)%json
          end do
          write(u, '(A)') '  }'
        end do
        write(u, '(A)') '] }'
        close(u)
      end subroutine harness_fortran_cpu__write_metrics_basis
      subroutine harness_fortran_cpu__write_diagnostics(results, n)
        type(harness_fortran_cpu__h_case_result), intent(in) :: results(:)
        integer, intent(in) :: n
        integer :: u, k, c
        open(newunit=u, file='diagnostics.json', status='replace')
        write(u, '(A)') '{ "per_case": {'
        do k = 1, n
          write(u, '(A)') '  "'//trim(results(k)%case_id)//'": {'
          do c = 1, size(results(k)%checks)
            write(u, '(A)') '    "'//trim(results(k)%checks(c)%id)//'": "'// &
              trim(results(k)%checks(c)%status)//'"'
          end do
          write(u, '(A)') '  }'
        end do
        write(u, '(A)') '} }'
        close(u)
      end subroutine harness_fortran_cpu__write_diagnostics
      subroutine harness_fortran_cpu__write_perf(case_id, target, steps, cells_updated, &
          walltime_sec, mpi_ranks, threads_per_rank, gpu_devices)
        character(len=*), intent(in) :: case_id
        character(len=*), intent(in) :: target
        integer, intent(in) :: steps
        integer, intent(in) :: cells_updated
        real(dp), intent(in) :: walltime_sec
        integer, intent(in) :: mpi_ranks
        integer, intent(in) :: threads_per_rank
        integer, intent(in) :: gpu_devices
        integer :: u
        open(newunit=u, file='perf.json', status='replace')
        write(u, '(A)') '{ "case_id": "'//trim(case_id)//'", "target": "'//trim(target)// &
          '", "steps": '//harness_fortran_cpu__emit_int(steps)//' }'
        close(u)
      end subroutine harness_fortran_cpu__write_perf
    end module harness_fortran_cpu_model
    """)

_CHECKS_STUB = textwrap.dedent("""\
    module dynamics_shallow_water_boundary_2d_periodic_copy_checks
      use, intrinsic :: iso_fortran_env, only: real64
      ! allow(C003)
      implicit none
      private
      integer, parameter :: dp = real64
      real(dp) :: ghost(4, 4) = 0.0_dp
      real(dp) :: interior(2, 2) = 0.0_dp
      real(dp) :: deviation = 0.0_dp
      real(dp) :: guard = 0.0_dp
      public :: case_setup, case_run, get_time
      public :: get_scalar, get_r1, get_r2, get_r3, get_r4
      public :: checks_compute, metric_compute
    contains
      subroutine case_setup(case_id, ok)
        character(len=*), intent(in) :: case_id
        logical, intent(out) :: ok
        integer :: i, j
        do i = 1, 2
          do j = 1, 2
            interior(i, j) = real(i * 10 + j, dp)
          end do
        end do
        ghost = 0.0_dp
        deviation = 0.0_dp
        guard = 0.0_dp
        ok = trim(case_id) /= 'l0_invalid_ny_xfail'
      end subroutine case_setup
      subroutine case_run(case_id, steps, cells_updated, ok)
        character(len=*), intent(in) :: case_id
        integer, intent(out) :: steps, cells_updated
        logical, intent(out) :: ok
        steps = 1
        cells_updated = 4
        ghost(2:3, 2:3) = interior
        if (trim(case_id) == 'l0_invalid_ny_xfail') then
          guard = 1.0_dp
          ok = .false.
        else
          ok = .true.
        end if
      end subroutine case_run
      subroutine get_time(t)
        real(dp), intent(out) :: t
        t = 0.0_dp
      end subroutine get_time
      subroutine get_scalar(name, val, found)
        character(len=*), intent(in) :: name
        real(dp), intent(out) :: val
        logical, intent(out) :: found
        found = .true.
        select case (trim(name))
        case ('max_abs_deviation')
          val = deviation
        case ('guard_fired')
          val = guard
        case default
          val = 0.0_dp
          found = .false.
        end select
      end subroutine get_scalar
      subroutine get_r1(name, arr, found)
        character(len=*), intent(in) :: name
        real(dp), allocatable, intent(out) :: arr(:)
        logical, intent(out) :: found
        allocate(arr(1))
        arr = 0.0_dp
        found = .false.
        if (len_trim(name) < 0) continue
      end subroutine get_r1
      subroutine get_r2(name, arr, found)
        character(len=*), intent(in) :: name
        real(dp), allocatable, intent(out) :: arr(:,:)
        logical, intent(out) :: found
        found = .true.
        select case (trim(name))
        case ('field_ghost')
          allocate(arr(4, 4))
          arr = ghost
        case ('field_interior')
          allocate(arr(2, 2))
          arr = interior
        case default
          allocate(arr(1, 1))
          arr = 0.0_dp
          found = .false.
        end select
      end subroutine get_r2
      subroutine get_r3(name, arr, found)
        character(len=*), intent(in) :: name
        real(dp), allocatable, intent(out) :: arr(:,:,:)
        logical, intent(out) :: found
        allocate(arr(1, 1, 1))
        arr = 0.0_dp
        found = .false.
        if (len_trim(name) < 0) continue
      end subroutine get_r3
      subroutine get_r4(name, arr, found)
        character(len=*), intent(in) :: name
        real(dp), allocatable, intent(out) :: arr(:,:,:,:)
        logical, intent(out) :: found
        allocate(arr(1, 1, 1, 1))
        arr = 0.0_dp
        found = .false.
        if (len_trim(name) < 0) continue
      end subroutine get_r4
      subroutine checks_compute(case_id, ncheck, check_ids, status)
        character(len=*), intent(in) :: case_id
        integer, intent(out) :: ncheck
        character(len=32), intent(out) :: check_ids(:)
        character(len=4), intent(out) :: status(:)
        select case (trim(case_id))
        case ('l0_periodic_x_wrap_pass')
          ncheck = 1
          check_ids(1) = 'x_wrap'
          status(1) = 'pass'
        case ('l0_periodic_y_wrap_pass')
          ncheck = 1
          check_ids(1) = 'y_wrap'
          status(1) = 'pass'
        case ('l0_invalid_ny_xfail')
          ncheck = 1
          check_ids(1) = 'input_guard'
          status(1) = 'fail'
        case default
          ncheck = 0
        end select
      end subroutine checks_compute
      subroutine metric_compute(case_id, name, val, is_na, reason_na, found)
        character(len=*), intent(in) :: case_id
        character(len=*), intent(in) :: name
        real(dp), intent(out) :: val
        logical, intent(out) :: is_na
        character(len=:), allocatable, intent(out) :: reason_na
        logical, intent(out) :: found
        val = 0.0_dp
        is_na = .false.
        reason_na = ''
        found = .false.
        if (len_trim(case_id) < 0 .or. len_trim(name) < 0) continue
      end subroutine metric_compute
    end module dynamics_shallow_water_boundary_2d_periodic_copy_checks
    """)


class RenderShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.txt = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS)

    def test_program_and_uses(self) -> None:
        self.assertIn(f"program {BOUNDARY_SID}_runner", self.txt)
        self.assertIn("use harness_fortran_cpu_model, only:", self.txt)
        self.assertIn(f"use {BOUNDARY_SID}_checks, only:", self.txt)
        # only the emitters for the ranks in use are imported (r2 + scalar; not r1/r3/r4)
        self.assertIn("harness_fortran_cpu__emit_array_r2", self.txt)
        self.assertIn("harness_fortran_cpu__emit_real", self.txt)
        self.assertNotIn("emit_array_r1", self.txt)
        self.assertNotIn("emit_array_r3", self.txt)
        self.assertNotIn("get_r1", self.txt)

    def test_calls_checks_abi(self) -> None:
        for name in ("case_setup", "case_run", "get_time", "checks_compute"):
            self.assertIn(f"call {name}(", self.txt)
        self.assertIn("call get_r2('field_ghost', r2buf, gfound)", self.txt)
        self.assertIn("call get_scalar('max_abs_deviation', sval, gfound)", self.txt)

    def test_xfail_flag(self) -> None:
        self.assertIn(
            "results(ci)%expected_xfail = trim(case_ids(ci)) == 'l0_invalid_ny_xfail'",
            self.txt)

    def test_no_metrics_block_when_absent(self) -> None:
        self.assertNotIn("metric_compute", self.txt)
        self.assertIn("allocate(results(ci)%metrics(0))", self.txt)

    def test_terminal_writers(self) -> None:
        self.assertIn("call harness_fortran_cpu__write_metrics_basis(mb_entries, 3)", self.txt)
        self.assertIn("call harness_fortran_cpu__write_diagnostics(results, ncases)", self.txt)
        self.assertIn("harness_fortran_cpu__write_perf(", self.txt)
        # perf parallelism: mpi=1, threads from IR (1), gpu=0
        self.assertIn("steps_total, cells_total, walltime, 1, 1, 0)", self.txt)

    def test_no_forbidden_outputs(self) -> None:
        for forbidden in ("verdict.json", "aggregate_verdict", "summary.json", "trial_meta"):
            self.assertNotIn(forbidden, self.txt)

    def test_line_width(self) -> None:
        over = [ln for ln in self.txt.splitlines() if len(ln) > 100]
        self.assertEqual(over, [], f"lines over 100 cols: {over}")

    def test_lint_shape_markers(self) -> None:
        self.assertIn("! allow(C003)", self.txt)
        self.assertIn("implicit none", self.txt)


class DeterminismTest(unittest.TestCase):
    def test_byte_identical(self) -> None:
        a = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS)
        b = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS)
        self.assertEqual(a, b)


class MetricsRenderTest(unittest.TestCase):
    def test_metric_compute_rendered(self) -> None:
        txt = render_runner(_metrics_ir(), "prob_x", HARNESS)
        self.assertIn("call metric_compute(trim(case_ids(ci)), 'error.l2',", txt)
        self.assertIn("call metric_compute(trim(case_ids(ci)), 'error.linf',", txt)
        self.assertIn("allocate(case_metrics(2))", txt)
        self.assertIn("results(ci)%metrics = case_metrics(1:mcount)", txt)
        # threads flow through to perf (num_threads=4)
        self.assertIn("walltime, 1, 4, 0)", txt)
        # rank-1 snapshot var -> get_r1 + emit_array_r1
        self.assertIn("call get_r1('u', r1buf, gfound)", txt)
        self.assertNotIn("get_r2", txt)


class RenderErrorMatrixTest(unittest.TestCase):
    def _expect(self, mutate) -> None:
        ir = copy.deepcopy(_boundary_ir())
        mutate(ir)
        with self.assertRaises(RenderError):
            render_runner(ir, BOUNDARY_SID, HARNESS)

    def test_rank_over_4(self) -> None:
        self._expect(lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
                     ["schema"]["variables"][0].__setitem__("shape_expr", "[2,2,2,2,2]"))

    def test_bad_shape_expr(self) -> None:
        self._expect(lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
                     ["schema"]["variables"][2].__setitem__("shape_expr", "banana"))

    def test_reserved_key_collision(self) -> None:
        self._expect(lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
                     ["schema"]["variables"][2].__setitem__("name", "t"))

    def test_verdict_fields_unsupported(self) -> None:
        self._expect(lambda ir: ir["io_contract"]["diagnostics_contract"]["verdict"]
                     .__setitem__("fields", ["overall", "failed_checks", "score"]))

    def test_two_infra_deps(self) -> None:
        self._expect(lambda ir: ir["dependency"]["direct_deps"].append(
            {"node_key": "infrastructure/other@1.0.0"}))

    def test_over_long_spec_id(self) -> None:
        with self.assertRaises(RenderError):
            render_runner(_boundary_ir(), "z" * 56, HARNESS)

    def test_required_raw_not_in_schema(self) -> None:
        self._expect(lambda ir: ir["io_contract"]["test_evidence_requirements"][0]
                     ["required_raw_variables"].append("ghost_field_typo"))

    def test_non_t_time_variable(self) -> None:
        # The harness writes the snapshot time under the fixed key 't'; a different
        # time_variable cannot be honored, so fail closed.
        self._expect(lambda ir: ir["io_contract"]["raw_requirements"]["required_evidence"][0]
                     ["schema"].__setitem__("time_variable", "tau"))

    def test_control_char_in_name(self) -> None:
        self._expect(lambda ir: ir["case"]["test_case_set"][0]
                     .__setitem__("case_id", "l0\nx"))

    def test_extreme_name_length_fails_closed(self) -> None:
        # A pathologically long case_id would push a rendered line past the 100-col lint limit;
        # since the runner is host-rendered (unrepairable by a leaf), fail closed at render time.
        def mut(ir: dict) -> None:
            long_id = "c_" + "x" * 95
            ir["case"]["test_case_set"][0]["case_id"] = long_id
            ir["io_contract"]["test_evidence_requirements"][0]["test_id"] = long_id
            ir["io_contract"]["test_predicates"][0]["test_id"] = long_id
            ir["io_contract"]["test_predicates"][0]["target_cases"] = [long_id]
        self._expect(mut)


class LineWidthTest(unittest.TestCase):
    """R1/M3c-β (review round 3): every rendered line must stay within the 100-col lint limit,
    including for long IR-sourced names (metric addresses, case_ids) — the hot lines are wrapped."""

    def _maxw(self, txt: str) -> int:
        return max(len(ln) for ln in txt.splitlines())

    def test_long_metric_address_wraps(self) -> None:
        ir = _metrics_ir()
        ir["io_contract"]["diagnostics_contract"]["metrics"] = ["convergence.observed_order.l2"]
        txt = render_runner(ir, "prob_x", HARNESS)
        self.assertLessEqual(self._maxw(txt), 100)
        # the metric_compute call is wrapped (address on the header, out-args on the next line)
        self.assertIn("'convergence.observed_order.l2', &", txt)

    def test_two_xfail_cases_multiline_expr_not_false_failed(self) -> None:
        # The `_xfail_expr` for >=2 xfail cases is a multi-line (`&`-continued) entry; the
        # column guard must measure per physical line, not the joined entry, else it wedges a
        # valid two-guard-case node into an unrepairable fail_closed.
        ir = _boundary_ir()
        ir["io_contract"]["test_predicates"][1]["expected_outcome"] = "xfail"
        txt = render_runner(ir, BOUNDARY_SID, HARNESS)
        self.assertLessEqual(self._maxw(txt), 100)
        self.assertIn(".or. &", txt)  # the two-term expression rendered as a continuation
        self.assertIn("== 'l0_periodic_y_wrap_pass'", txt)

    def test_long_case_id_target_wraps(self) -> None:
        ir = _boundary_ir()
        long_id = "l0_periodic_x_wrap_with_a_fairly_long_descriptive_name_pass"  # 58 chars
        ir["case"]["test_case_set"][0]["case_id"] = long_id
        ir["io_contract"]["test_evidence_requirements"][0]["test_id"] = long_id
        ir["io_contract"]["test_predicates"][0]["test_id"] = long_id
        ir["io_contract"]["test_predicates"][0]["target_cases"] = [long_id]
        txt = render_runner(ir, BOUNDARY_SID, HARNESS)
        self.assertLessEqual(self._maxw(txt), 100)


class FortranLiteralEscapingTest(unittest.TestCase):
    """R1/M3c-β (Codex review): IR-sourced names are only required non-empty, so a name with a
    single quote must be doubled (`''`) in the generated Fortran literal, not break it."""

    def test_apostrophe_in_names_is_escaped(self) -> None:
        ir = _boundary_ir()
        ir["case"]["test_case_set"][0]["case_id"] = "l0_x'wrap"
        ir["io_contract"]["test_evidence_requirements"][0]["test_id"] = "l0_x'wrap"
        ir["io_contract"]["test_predicates"][0]["test_id"] = "l0_x'wrap"
        ir["io_contract"]["test_predicates"][0]["target_cases"] = ["l0_x'wrap"]
        txt = render_runner(ir, BOUNDARY_SID, HARNESS)
        self.assertIn("case ('l0_x''wrap')", txt)
        # no broken (unescaped) literal
        self.assertNotIn("case ('l0_x'wrap')", txt)

    @unittest.skipUnless(_HAVE_GFORTRAN, "gfortran not available")
    def test_escaped_runner_compiles(self) -> None:
        # An apostrophe in a case_id must produce a valid (doubled-quote) Fortran literal that
        # compiles + links against the harness/checks stubs, not a broken literal.
        ir = _boundary_ir()
        for c in ir["case"]["test_case_set"]:
            c["case_id"] = c["case_id"].replace("l0_", "l0'")
        for r in ir["io_contract"]["test_evidence_requirements"]:
            r["test_id"] = r["test_id"].replace("l0_", "l0'")
        for p in ir["io_contract"]["test_predicates"]:
            p["test_id"] = p["test_id"].replace("l0_", "l0'")
            p["target_cases"] = [tc.replace("l0_", "l0'") for tc in p["target_cases"]]
        runner = render_runner(ir, BOUNDARY_SID, HARNESS)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "harness_fortran_cpu_model.f90").write_text(_HARNESS_STUB)
            (d / f"{BOUNDARY_SID}_checks.f90").write_text(_CHECKS_STUB)
            (d / f"{BOUNDARY_SID}_runner.f90").write_text(runner)
            for srcs in (["harness_fortran_cpu_model.f90"], [f"{BOUNDARY_SID}_checks.f90"],
                         [f"{BOUNDARY_SID}_runner.f90"]):
                r = subprocess.run(["gfortran", "-std=f2008", "-c", *srcs],
                                   cwd=d, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)


class HarnessPinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ir = _boundary_ir()
        self.sigs = _harness_signatures()
        self.src = _HARNESS_STUB

    def test_clean_pin(self) -> None:
        assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, self.src)

    def test_wrong_harness_id(self) -> None:
        with self.assertRaises(RenderError):
            assert_harness_pin(self.ir, BOUNDARY_SID, "harness_other", self.sigs, self.src)

    def test_ir_signature_drift(self) -> None:
        bad = [dict(e) for e in self.sigs]
        for e in bad:
            if e["symbol"].endswith("__write_snapshot"):
                e["interface"] = e["interface"].replace("intent(in) :: time",
                                                         "intent(in) :: tstamp")
        with self.assertRaises(RenderError):
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, bad, self.src)

    def test_source_signature_drift(self) -> None:
        bad_src = self.src.replace(
            "subroutine harness_fortran_cpu__parse_cases(tokens, ntokens, case_ids, ncases, ok)",
            "subroutine harness_fortran_cpu__parse_cases(tokens, ntokens, case_ids, nc, ok)")
        with self.assertRaises(RenderError):
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, self.sigs, bad_src)

    def test_missing_symbol_in_ir(self) -> None:
        pruned = [e for e in self.sigs if not e["symbol"].endswith("__box")]
        with self.assertRaises(RenderError):
            assert_harness_pin(self.ir, BOUNDARY_SID, HARNESS, pruned, self.src)


@unittest.skipUnless(_HAVE_GFORTRAN, "gfortran not available")
class GfortranSmokeTest(unittest.TestCase):
    def test_rendered_runner_compiles_and_runs(self) -> None:
        runner = render_runner(_boundary_ir(), BOUNDARY_SID, HARNESS)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "harness_fortran_cpu_model.f90").write_text(_HARNESS_STUB)
            (d / f"{BOUNDARY_SID}_checks.f90").write_text(_CHECKS_STUB)
            (d / f"{BOUNDARY_SID}_runner.f90").write_text(runner)

            def fc(*srcs: str) -> None:
                r = subprocess.run(
                    ["gfortran", "-std=f2008", "-c", *srcs],
                    cwd=d, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

            fc("harness_fortran_cpu_model.f90")
            fc(f"{BOUNDARY_SID}_checks.f90")
            fc(f"{BOUNDARY_SID}_runner.f90")
            link = subprocess.run(
                ["gfortran", "harness_fortran_cpu_model.o",
                 f"{BOUNDARY_SID}_checks.o", f"{BOUNDARY_SID}_runner.o", "-o", "runner"],
                cwd=d, capture_output=True, text=True)
            self.assertEqual(link.returncode, 0, link.stderr)

            (d / "raw" / "state_snapshots").mkdir(parents=True)
            run = subprocess.run(
                ["./runner", "--cases", "spec.ir.yaml",
                 "l0_periodic_x_wrap_pass", "l0_periodic_y_wrap_pass", "l0_invalid_ny_xfail"],
                cwd=d, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            # every case emitted its own snapshot; diagnostics + perf + metrics_basis exist
            for cid in ("l0_periodic_x_wrap_pass", "l0_periodic_y_wrap_pass",
                        "l0_invalid_ny_xfail"):
                self.assertTrue((d / "raw" / "state_snapshots" / f"{cid}.json").is_file())
            self.assertTrue((d / "diagnostics.json").is_file())
            self.assertTrue((d / "perf.json").is_file())
            self.assertTrue((d / "raw" / "metrics_basis.json").is_file())
            diag = (d / "diagnostics.json").read_text()
            self.assertIn("input_guard", diag)


if __name__ == "__main__":
    unittest.main()
