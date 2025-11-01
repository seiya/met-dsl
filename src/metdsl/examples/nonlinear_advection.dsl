# Nonlinear advection-diffusion solver example
MODEL nonlinear_advection
VERSION 1.0.0
FIELD velocity_u staggered:x edge
FIELD velocity_v staggered:y edge
FIELD tracer staggered:center cell
STENCIL advect_flux scheme=nonlinear_centered order=2 fields=velocity_u,velocity_v,tracer
STENCIL diffuse_tracer scheme=laplacian order=2 fields=tracer
RK4_STAGE stage1 compute_fluxes
RK4_STAGE stage2 accumulate_fluxes
RK4_STAGE stage3 predictor_corrector
RK4_STAGE stage4 finalize_state
