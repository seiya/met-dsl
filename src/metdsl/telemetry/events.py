from __future__ import annotations

import json
import os
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

from pydantic import BaseModel, Field


class TelemetryEvent(BaseModel):
    """
    Structured telemetry event written as NDJSON for Principle V compliance.
    """

    event: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    payload: Dict[str, Any] = Field(default_factory=dict)

    def model_dump_ndjson(self) -> str:
        data = self.dict()
        data["timestamp"] = self.timestamp.isoformat()
        return json.dumps(data, separators=(",", ":"))


class NDJSONTelemetryLogger:
    """
    Writes telemetry events to an NDJSON file, creating directories as needed.
    """

    def __init__(self, sink: Path) -> None:
        self.sink = sink
        self._ensure_directory()

    def _ensure_directory(self) -> None:
        self.sink.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: TelemetryEvent) -> None:
        with self.sink.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_ndjson())
            fh.write("\n")


class TelemetryEmitter:
    """
    Emits telemetry events to a primary sink and falls back to a local file if an error occurs.
    """

    def __init__(self, primary_sink: Path, fallback_sink: Optional[Path] = None) -> None:
        self.primary = NDJSONTelemetryLogger(primary_sink)
        self.fallback = (
            NDJSONTelemetryLogger(fallback_sink) if fallback_sink is not None else None
        )

    def emit(self, event: Union[str, "SolverLifecycleEvent"], **payload: Any) -> None:
        event_name = event.value if isinstance(event, Enum) else event
        telemetry_event = TelemetryEvent(event=event_name, payload=payload)
        try:
            self.primary.record(telemetry_event)
        except OSError:
            if not self.fallback:
                raise
            self.fallback.record(
                TelemetryEvent(
                    event=f"{event_name}_fallback",
                    payload={"original": payload, "reason": "primary_sink_unavailable"},
                )
            )


class SolverLifecycleEvent(str, Enum):
    """
    Canonical solver lifecycle telemetry events emitted by CLI workflows.
    """

    SPEC_CREATED = "solver.spec.created"
    SPEC_CLONED = "solver.spec.cloned"
    GENERATION_STARTED = "solver.generation.started"
    GENERATION_COMPLETED = "solver.generation.completed"
    GENERATION_FAILED = "solver.generation.failed"
    VALIDATION_STARTED = "solver.validation.started"
    VALIDATION_COMPLETED = "solver.validation.completed"
    VALIDATION_FAILED = "solver.validation.failed"
    TIMESTEP_WARNING = "solver.validation.timestep_warning"
    COMPLETENESS_ERROR = "solver.validation.completeness_error"
    ONBOARDING_DURATION_RECORDED = "solver.onboarding.session_recorded"
    PILOT_FEEDBACK_CAPTURED = "solver.feedback.pilot_recorded"


__all__ = [
    "TelemetryEvent",
    "NDJSONTelemetryLogger",
    "TelemetryEmitter",
    "SolverLifecycleEvent",
]
