#!/usr/bin/env python3
"""Lightweight syntax checker for workflow JSON/YAML artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _default_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    raise ValueError(f"{path}: unsupported extension")


def _check_top_level(path: Path, data: Any, expected_top: str | None) -> None:
    if expected_top is None:
        return
    if expected_top == "object":
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top-level must be object/mapping")
        return
    if expected_top == "array":
        if not isinstance(data, list):
            raise ValueError(f"{path}: top-level must be array/sequence")
        return
    raise ValueError(f"unsupported expected top-level: {expected_top}")


def check_file(path: Path, *, fmt: str | None, expected_top: str | None) -> None:
    actual_format = _default_format(path) if fmt is None or fmt == "auto" else fmt
    if actual_format == "json":
        data = _load_json(path)
    elif actual_format == "yaml":
        data = _load_yaml(path)
    else:
        raise ValueError(f"{path}: unsupported format ({actual_format})")
    _check_top_level(path, data, expected_top)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check syntax of workflow JSON/YAML artifacts."
    )
    parser.add_argument(
        "--format",
        choices=("auto", "json", "yaml"),
        default="auto",
        help="Artifact format. Default detects from file extension.",
    )
    parser.add_argument(
        "--expect-top",
        choices=("object", "array"),
        default=None,
        help="Require top-level JSON/YAML type.",
    )
    parser.add_argument("paths", nargs="+", help="Artifact paths to check.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    failures: list[str] = []
    for raw_path in args.paths:
        path = Path(raw_path)
        if not path.exists():
            failures.append(f"{path}: missing")
            continue
        if not path.is_file():
            failures.append(f"{path}: not a file")
            continue
        try:
            check_file(path, fmt=args.format, expected_top=args.expect_top)
        except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
            failures.append(str(exc))

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1

    for raw_path in args.paths:
        print(f"PASS: {raw_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
