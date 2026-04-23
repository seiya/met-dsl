#!/usr/bin/env python3
"""Hook adapters by backend."""

from tools.hooks.adapters.claude import ClaudeHookAdapter
from tools.hooks.adapters.codex import CodexHookAdapter

__all__ = ["ClaudeHookAdapter", "CodexHookAdapter"]
