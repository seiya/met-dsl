# TODO
This document is the canonical source that aggregates incomplete tasks managed across the whole repository.

## TODO list

- Once the Claude backend can obtain `session_id` or `agent_session_id` from the hook payload, abolish the `agent_run_id` resolution that depends on `active_child_agent_run_id.txt`, and unify it to the same session-identifier-based resolution as the Codex backend.
  - Removal targets: the active-file management helpers and Claude-specific branches in `tools/orchestration_runtime.py`, the active-file reference branch in `tools/hooks/cli.py`, and the related tests that assume the active file.
  - Completion criterion: `agent_run_id` can be uniquely resolved using only the session identifier from the hook payload on both Claude and Codex, and it has been verified by tests that no active file is generated.
