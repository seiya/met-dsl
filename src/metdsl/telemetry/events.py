from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

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

    def emit(self, event: str, **payload: Any) -> None:
        telemetry_event = TelemetryEvent(event=event, payload=payload)
        try:
            self.primary.record(telemetry_event)
        except OSError:
            if not self.fallback:
                raise
            self.fallback.record(
                TelemetryEvent(
                    event=f"{event}_fallback",
                    payload={"original": payload, "reason": "primary_sink_unavailable"},
                )
            )


__all__ = ["TelemetryEvent", "NDJSONTelemetryLogger", "TelemetryEmitter"]
