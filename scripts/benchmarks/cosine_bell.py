from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Tuple


def _load_json(path: Path) -> Dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _evaluate_metrics(results: Dict[str, object]) -> Tuple[float, float]:
    if "metrics" in results and isinstance(results["metrics"], dict):
        metrics = results["metrics"]  # type: ignore[assignment]
    else:
        metrics = results
    max_error = float(metrics.get("max_absolute_error", 0.0))
    drift = float(metrics.get("conservation_drift", 0.0))
    return max_error, drift


def analyze(manifest: Path, outputs: Path) -> Dict[str, object]:
    manifest_data = _load_json(manifest)
    results_data = _load_json(outputs)

    tolerance = (
        manifest_data.get("analysis", {}).get("tolerance", {})
        if isinstance(manifest_data.get("analysis"), dict)
        else {}
    )
    threshold_error = float(tolerance.get("max_absolute_error", 1.0))
    threshold_drift = float(tolerance.get("conservation_drift", 1.0))

    max_error, drift = _evaluate_metrics(results_data)
    status = "passed" if max_error <= threshold_error and drift <= threshold_drift else "failed"

    return {
        "benchmark": manifest_data.get("analysis", {}).get("benchmark"),
        "metrics": {
            "max_absolute_error": max_error,
            "conservation_drift": drift,
        },
        "tolerance": {
            "max_absolute_error": threshold_error,
            "conservation_drift": threshold_drift,
        },
        "status": status,
        "metadata": {
            "spec_id": manifest_data.get("metadata", {}).get("spec_id"),
            "config_hash": manifest_data.get("config_hash"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Rotating cosine bell benchmark analysis.")
    parser.add_argument("--manifest", type=Path, required=True, help="Path to solver manifest JSON.")
    parser.add_argument("--outputs", type=Path, required=True, help="Path to solver results JSON.")
    args = parser.parse_args()

    payload = analyze(args.manifest, args.outputs)
    print(json.dumps(payload, indent=2))

    if payload["status"] != "passed":
        sys.exit(1)


if __name__ == "__main__":
    main()
