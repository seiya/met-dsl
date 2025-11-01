from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from metdsl.telemetry.events import SolverLifecycleEvent, TelemetryEmitter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record onboarding duration telemetry.")
    parser.add_argument("--spec-id", required=True)
    parser.add_argument("--version-id", required=True)
    parser.add_argument("--start", required=True, help="ISO timestamp or minutes (e.g., 2025-11-01T09:00Z or 30m)")
    parser.add_argument("--end", required=True, help="ISO timestamp or minutes (e.g., 2025-11-01T09:30Z or 0m)")
    parser.add_argument("--notes", default="")
    parser.add_argument("--telemetry-sink", type=Path, default=Path("build/logs/onboarding.ndjson"))
    return parser.parse_args()


def parse_time(label: str, value: str) -> datetime:
    try:
        if value.endswith("m"):
            minutes = float(value[:-1])
            return datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)
        return datetime.fromisoformat(value)
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError(f"Invalid {label} value: {value}") from exc


def main() -> None:
    args = parse_args()
    emitter = TelemetryEmitter(primary_sink=args.telemetry_sink)

    start = parse_time("start", args.start)
    end = parse_time("end", args.end)
    duration_minutes = (end - start).total_seconds() / 60.0

    emitter.emit(
        SolverLifecycleEvent.ONBOARDING_DURATION_RECORDED,
        spec_id=args.spec_id,
        version_id=args.version_id,
        duration_minutes=duration_minutes,
        notes=args.notes,
    )

    payload = {
        "spec_id": args.spec_id,
        "version_id": args.version_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "duration_minutes": duration_minutes,
        "notes": args.notes,
    }
    out_path = Path("docs/feedback/onboarding_sessions") / f"{args.spec_id}_{args.version_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
