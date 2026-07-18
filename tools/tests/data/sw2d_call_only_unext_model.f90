module shallow_water2d_model
  use, intrinsic :: iso_fortran_env, only: dp => real64
  use dynamics_shallow_water_boundary_2d_periodic_copy_model, only: &
    dynamics_shallow_water_boundary_2d_periodic_copy__apply
  use dynamics_shallow_water_flux_2d_rusanov_p0_model, only: &
    dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux
  use dynamics_shallow_water_time_update_2d_ssprk2_model, only: &
    dynamics_shallow_water_time_update_2d_ssprk2__advance
  ! allow(C003)
  implicit none
  private
  public :: shallow_water2d__advance
contains
  subroutine shallow_water2d__advance(h, hu, hv, z_b, g, nx, ny, ng, &
      dt, dx, dy, h_out, hu_out, hv_out, guard_ok)
    real(dp), intent(in) :: h(:,:), hu(:,:), hv(:,:), z_b(:,:)
    real(dp), intent(in) :: g, dt, dx, dy
    integer, intent(in) :: nx, ny, ng
    real(dp), intent(out) :: h_out(:,:), hu_out(:,:), hv_out(:,:)
    logical, intent(out) :: guard_ok
    integer :: nxg, nyg, i, j, ih, jh, ir, jr, il, jl
    real(dp), allocatable :: wh(:,:), whu(:,:), whv(:,:)
    real(dp), allocatable :: bh(:,:), bhu(:,:), bhv(:,:)
    real(dp), allocatable :: uc(:,:), vc(:,:)
    real(dp), allocatable :: hxl(:,:), hxr(:,:), hyb(:,:), hyt(:,:)
    real(dp), allocatable :: fx(:,:,:), gy(:,:,:)
    real(dp), allocatable :: lflux(:,:,:), sb(:,:,:)
    real(dp), allocatable :: un(:,:,:), ustage(:,:,:), unext(:,:,:)
    real(dp) :: ul(3), ur(3), ub(3), ut(3), fstar(3), gstar(3)
    real(dp) :: ax, ay, gpass, zr, zl, zt, zbb
    logical :: gb1, gb2, gb3, gtime, wet
    nxg = nx + 2*ng
    nyg = ny + 2*ng
    allocate(wh(nxg,nyg), whu(nxg,nyg), whv(nxg,nyg))
    allocate(bh(nxg,nyg), bhu(nxg,nyg), bhv(nxg,nyg))
    wh = 0.0_dp
    whu = 0.0_dp
    whv = 0.0_dp
    wh(ng+1:ng+nx, ng+1:ng+ny) = h
    whu(ng+1:ng+nx, ng+1:ng+ny) = hu
    whv(ng+1:ng+nx, ng+1:ng+ny) = hv
    call dynamics_shallow_water_boundary_2d_periodic_copy__apply( &
      wh, nx, ny, ng, bh, gb1)
    call dynamics_shallow_water_boundary_2d_periodic_copy__apply( &
      whu, nx, ny, ng, bhu, gb2)
    call dynamics_shallow_water_boundary_2d_periodic_copy__apply( &
      whv, nx, ny, ng, bhv, gb3)
    allocate(uc(nxg,nyg), vc(nxg,nyg))
    uc = 0.0_dp
    vc = 0.0_dp
    do j = 1, nyg
      do i = 1, nxg
        if (bh(i,j) > 0.0_dp) then
          uc(i,j) = bhu(i,j) / bh(i,j)
          vc(i,j) = bhv(i,j) / bh(i,j)
        end if
      end do
    end do
    allocate(hxl(nx,ny), hxr(nx,ny), hyb(nx,ny), hyt(nx,ny))
    wet = .true.
    do j = 1, ny
      jh = j + ng
      do i = 1, nx
        ih = i + ng
        zr = max(z_b(ih,jh), z_b(ih+1,jh))
        zl = max(z_b(ih-1,jh), z_b(ih,jh))
        zt = max(z_b(ih,jh), z_b(ih,jh+1))
        zbb = max(z_b(ih,jh-1), z_b(ih,jh))
        hxl(i,j) = max(0.0_dp, bh(ih,jh) + z_b(ih,jh) - zr)
        hxr(i,j) = max(0.0_dp, bh(ih,jh) + z_b(ih,jh) - zl)
        hyb(i,j) = max(0.0_dp, bh(ih,jh) + z_b(ih,jh) - zt)
        hyt(i,j) = max(0.0_dp, bh(ih,jh) + z_b(ih,jh) - zbb)
        if (hxl(i,j) <= 0.0_dp .or. hxr(i,j) <= 0.0_dp) wet = .false.
        if (hyb(i,j) <= 0.0_dp .or. hyt(i,j) <= 0.0_dp) wet = .false.
      end do
    end do
    allocate(fx(3,nx,ny), gy(3,nx,ny))
    gpass = 1.0_dp
    do j = 1, ny
      jh = j + ng
      jr = mod(j, ny) + 1
      do i = 1, nx
        ih = i + ng
        ir = mod(i, nx) + 1
        ul(1) = hxl(i,j)
        ul(2) = hxl(i,j) * uc(ih,jh)
        ul(3) = hxl(i,j) * vc(ih,jh)
        ur(1) = hxr(ir,j)
        ur(2) = hxr(ir,j) * uc(ir+ng,jh)
        ur(3) = hxr(ir,j) * vc(ir+ng,jh)
        ub(1) = hyb(i,j)
        ub(2) = hyb(i,j) * uc(ih,jh)
        ub(3) = hyb(i,j) * vc(ih,jh)
        ut(1) = hyt(i,jr)
        ut(2) = hyt(i,jr) * uc(ih,jr+ng)
        ut(3) = hyt(i,jr) * vc(ih,jr+ng)
        call dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux( &
          ul, ur, ub, ut, g, fstar, gstar, ax, ay, gpass)
        fx(:,i,j) = fstar
        gy(:,i,j) = gstar
        if (gpass <= 0.0_dp) wet = .false.
      end do
    end do
    allocate(lflux(3,nx,ny), sb(3,nx,ny))
    do j = 1, ny
      jl = mod(j-2+ny, ny) + 1
      do i = 1, nx
        il = mod(i-2+nx, nx) + 1
        lflux(:,i,j) = -(fx(:,i,j) - fx(:,il,j)) / dx &
          - (gy(:,i,j) - gy(:,i,jl)) / dy
        sb(1,i,j) = 0.0_dp
        sb(2,i,j) = g / (2.0_dp*dx) * (hxl(i,j)**2 - hxr(i,j)**2)
        sb(3,i,j) = g / (2.0_dp*dy) * (hyb(i,j)**2 - hyt(i,j)**2)
      end do
    end do
    allocate(un(3,nx,ny), ustage(3,nx,ny), unext(3,nx,ny))
    un(1,:,:) = bh(ng+1:ng+nx, ng+1:ng+ny)
    un(2,:,:) = bhu(ng+1:ng+nx, ng+1:ng+ny)
    un(3,:,:) = bhv(ng+1:ng+nx, ng+1:ng+ny)
    call dynamics_shallow_water_time_update_2d_ssprk2__advance( &
      un, lflux, sb, z_b(ng+1:ng+nx, ng+1:ng+ny), dt, dx, dy, &
      ustage, unext, gtime)
    h_out = unext(1,:,:)
    hu_out = unext(2,:,:)
    hv_out = unext(3,:,:)
    guard_ok = gb1 .and. gb2 .and. gb3 .and. gtime .and. wet
  end subroutine shallow_water2d__advance
end module shallow_water2d_model
