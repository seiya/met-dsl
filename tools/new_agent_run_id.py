#!/usr/bin/env python3
"""Mint a fresh UUID4 for use as `agent_run_id`.

Canonical UUID source for orchestration agents that must pre-generate child
`agent_run_id` values before `record-launch`. Kept as a minimal standalone
script (only stdlib `uuid`) so an unrelated import or syntax break in
`tools/orchestration_runtime.py` cannot block child launch.

Replaces, for the orchestration agent's UUID needs:
  - `cat /proc/sys/kernel/random/uuid` (blocked by Claude Code session sandbox)
  - `uuidgen`                            (blocked by session sandbox: requires approval)
  - `python3 -c 'import uuid; ...'`      (blocked by `forbid_python_inline_write`)
"""

from __future__ import annotations

import sys
import uuid


def main() -> int:
    print(str(uuid.uuid4()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
