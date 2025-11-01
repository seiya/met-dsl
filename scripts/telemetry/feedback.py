from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from metdsl.telemetry.events import SolverLifecycleEvent, TelemetryEmitter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record pilot feedback telemetry.")
    parser.add_argument("--spec-id", required=True)
    parser.add_argument("--version-id", required=True)
    parser.add_argument("--participant", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--telemetry-sink", type=Path, default=Path("build/logs/feedback.ndjson"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    emitter = TelemetryEmitter(primary_sink=args.telemetry_sink)

    event_payload = {
        "spec_id": args.spec_id,
        "version_id": args.version_id,
        "participant": args.participant,
        "summary": args.summary,
        "captured_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    emitter.emit(SolverLifecycleEvent.PILOT_FEEDBACK_CAPTURED, **event_payload)

    out_path = Path("docs/feedback/pilot_reports") / f"{args.spec_id}_{args.version_id}_{args.participant}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(event_payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
