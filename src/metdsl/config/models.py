from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, PositiveFloat, PositiveInt, root_validator


class Stage(str, Enum):
    IR = "ir"
    FORTRAN2003 = "fortran2003"
    VERIFY = "verify"
    LIST_TARGETS = "list_targets"


class Target(str, Enum):
    FORTRAN2003 = "fortran2003"
    EXPERIMENTAL = "experimental"


class OptimizationPreset(str, Enum):
    BASELINE = "baseline"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class CompilerFlagOverrides(BaseModel):
    gfortran: Optional[List[str]] = None
    oneapi: Optional[List[str]] = None
    nvfortran: Optional[List[str]] = None


class Staggering(str, Enum):
    ARAKAWA_C = "arakawa_c"


class BoundaryType(str, Enum):
    PERIODIC = "periodic"


class GridConfig(BaseModel):
    nx: PositiveInt = Field(default=128, description="Number of cells along the x-axis")
    ny: PositiveInt = Field(default=128, description="Number of cells along the y-axis")
    dx: PositiveFloat = Field(default=1.0, description="Cell spacing along the x-axis")
    dy: PositiveFloat = Field(default=1.0, description="Cell spacing along the y-axis")
    staggering: Staggering = Field(
        default=Staggering.ARAKAWA_C,
        description="Grid staggering convention applied to the field variables",
    )


class BoundaryConditions(BaseModel):
    x: BoundaryType = Field(default=BoundaryType.PERIODIC, description="Boundary for x-axis")
    y: BoundaryType = Field(default=BoundaryType.PERIODIC, description="Boundary for y-axis")


class RK4Config(BaseModel):
    total_steps: PositiveInt = Field(default=100, description="Number of RK4 timesteps to execute")
    time_step: PositiveFloat = Field(default=60.0, description="Simulation time step size")
    stability_limit: Optional[PositiveFloat] = Field(
        default=None,
        description="Optional CFL-like stability limit for warning users about large timesteps",
    )
    stage_labels: List[str] = Field(
        default_factory=lambda: ["k1", "k2", "k3", "k4"],
        description="Names used for RK4 intermediate stages",
    )


class EmissionConfig(BaseModel):
    target: Target = Field(default=Target.FORTRAN2003)
    optimization_preset: OptimizationPreset = Field(default=OptimizationPreset.BALANCED)
    compiler_overrides: CompilerFlagOverrides = Field(default_factory=CompilerFlagOverrides)
    discovery_only: bool = False
    metadata: Dict[str, str] = Field(default_factory=dict)
    telemetry_sink: Optional[Path] = None
    grid: GridConfig = Field(default_factory=GridConfig)
    boundary_conditions: BoundaryConditions = Field(default_factory=BoundaryConditions)
    rk4: RK4Config = Field(default_factory=RK4Config)

    @root_validator
    def validate_target_rules(cls, values: Dict[str, object]) -> Dict[str, object]:
        target = values.get("target")
        discovery_only = values.get("discovery_only")
        if target != Target.FORTRAN2003 and not discovery_only:
            raise ValueError(
                "Non-Fortran targets are discovery-only. Set discovery_only=true for experimental targets."
            )
        return values

    class Config:
        json_encoders = {Path: lambda value: str(value)}


__all__ = [
    "Stage",
    "Target",
    "OptimizationPreset",
    "CompilerFlagOverrides",
    "Staggering",
    "BoundaryType",
    "GridConfig",
    "BoundaryConditions",
    "RK4Config",
    "EmissionConfig",
]
