from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, root_validator


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


class EmissionConfig(BaseModel):
    target: Target = Field(default=Target.FORTRAN2003)
    optimization_preset: OptimizationPreset = Field(default=OptimizationPreset.BALANCED)
    compiler_overrides: CompilerFlagOverrides = Field(default_factory=CompilerFlagOverrides)
    discovery_only: bool = False
    metadata: Dict[str, str] = Field(default_factory=dict)
    telemetry_sink: Optional[Path] = None

    @root_validator
    def validate_target_rules(cls, values: Dict[str, object]) -> Dict[str, object]:
        target = values.get("target")
        discovery_only = values.get("discovery_only", False)
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
    "EmissionConfig",
]
