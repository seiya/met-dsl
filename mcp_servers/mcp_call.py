#!/usr/bin/env python3
"""Minimal MCP client for build_runtime_server.py."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


def _write_message(stream, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    stream.write(header)
    stream.write(body)
    stream.flush()


def _read_message(stream) -> dict[str, Any]:
    while True:
        first = stream.readline()
        if not first:
            raise RuntimeError("unexpected EOF while reading MCP message header")
        if not first.strip():
            continue
        if first.lower().startswith(b"content-length:"):
            length = int(first.split(b":", 1)[1].strip())
            while True:
                header_line = stream.readline()
                if not header_line:
                    raise RuntimeError("unexpected EOF while reading MCP headers")
                if header_line in (b"\r\n", b"\n"):
                    break
            body = stream.read(length)
            if not body:
                raise RuntimeError("unexpected EOF while reading MCP body")
            return json.loads(body.decode("utf-8"))
        return json.loads(first.decode("utf-8"))


def _mcp_call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    proc = subprocess.Popen(
        [sys.executable, "mcp_servers/build_runtime_server.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None

    try:
        _write_message(
            proc.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
            },
        )
        _ = _read_message(proc.stdout)

        _write_message(
            proc.stdin,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )

        _write_message(
            proc.stdin,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        response = _read_message(proc.stdout)
    finally:
        proc.kill()
        proc.wait(timeout=2)

    if "error" in response:
        raise RuntimeError(response["error"])
    result = response.get("result", {})
    if result.get("isError"):
        structured = result.get("structuredContent", {})
        raise RuntimeError(json.dumps(structured, ensure_ascii=False))
    return result.get("structuredContent", {})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True)
    parser.add_argument("--args-json", required=True)
    args = parser.parse_args()

    tool_args = json.loads(args.args_json)
    data = _mcp_call(args.tool, tool_args)
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
