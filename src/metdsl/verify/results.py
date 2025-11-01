from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class CompilerResult:
    compiler: str
    exit_code: int
    stdout: str
    stderr: str


def collect_results(results: List[CompilerResult]) -> dict:
    return {
        "total": len(results),
        "success": sum(1 for r in results if r.exit_code == 0),
        "failures": [r for r in results if r.exit_code != 0],
    }
