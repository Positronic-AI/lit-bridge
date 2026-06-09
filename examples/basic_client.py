#!/usr/bin/env python3
"""Basic lit-bridge client — reference implementation.

Connects to a lit-bridge daemon, creates a Claude Code session,
sends messages, and prints streamed responses. Demonstrates the
JSON-lines protocol and key patterns for a working integration.

Usage:
  # Start the bridge daemon first:
  python3 server.py --socket /tmp/lit-bridge.sock

  # Then run this client:
  python3 examples/basic_client.py

  # Or send a one-shot message:
  python3 examples/basic_client.py "What files are in this directory?"
"""

import asyncio
import json
import os
import sys

SOCKET_PATH = os.environ.get("LIT_BRIDGE_SOCKET", "/tmp/lit-bridge.sock")
SESSION_NAME = "example"
CLI = "claude"
CLI_ARGS = ["--model", "sonnet", "--dangerously-skip-permissions"]
WORKING_DIR = "/tmp"


async def connect(socket_path: str):
    """Connect to the lit-bridge daemon socket."""
    reader, writer = await asyncio.open_unix_connection(socket_path)

    # The daemon sends monitor_ready as the first event
    line = await asyncio.wait_for(reader.readline(), timeout=10)
    event = json.loads(line)
    assert event["event"] == "monitor_ready", f"unexpected: {event}"
    print(f"Connected ({event.get('sessions', 0)} existing sessions)")

    return reader, writer


async def send(writer, cmd: dict):
    """Send a JSON command to the daemon."""
    writer.write((json.dumps(cmd) + "\n").encode())
    await writer.drain()


async def read_event(reader, timeout=300.0) -> dict:
    """Read one JSON event from the daemon."""
    line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    if not line:
        raise ConnectionError("daemon disconnected")
    return json.loads(line)


async def create_session(reader, writer) -> bool:
    """Create or reuse a CLI session. Returns True if newly created."""
    await send(writer, {
        "cmd": "create",
        "session": SESSION_NAME,
        "cli": CLI,
        "parser": "claude-code",
        "args": CLI_ARGS,
        "working_dir": WORKING_DIR,
    })

    # Drain stale events (metadata, state changes from prior turns)
    # until we get the ready event. This is critical — the daemon
    # reuses the event queue across calls, so leftover events from
    # previous turns may arrive before "ready".
    while True:
        event = await read_event(reader, timeout=30)
        if event.get("event") == "ready":
            return not event.get("reused", False)
        if event.get("event") == "error":
            raise RuntimeError(event.get("message"))
        # Skip stale events silently


async def send_message(reader, writer, content: str) -> str:
    """Send a message and stream the response. Returns final content."""
    is_new = await create_session(reader, writer)
    print(f"Session {'created' if is_new else 'reused'}")

    await send(writer, {
        "cmd": "send",
        "session": SESSION_NAME,
        "content": content,
    })

    response = ""
    while True:
        event = await read_event(reader, timeout=300)
        event_type = event.get("event")

        if event_type == "state":
            state = event.get("to")
            if state == "thinking":
                print("Thinking...", end="", flush=True)
            elif state == "responding":
                print("\r", end="")

        elif event_type == "replace":
            # Full content replacement — the daemon sends the complete
            # response text on each poll, not incremental deltas.
            response = event.get("text", "")

        elif event_type in ("complete", "paused"):
            # "complete" for standalone sessions, "paused" for channel
            # sessions (which stay alive between turns).
            final = event.get("content", "")
            if final:
                response = final
            break

        elif event_type == "tool_use":
            name = event.get("name", "")
            print(f"  [tool] {name}")

        elif event_type == "tool_result":
            content_len = len(event.get("content", ""))
            print(f"  [result] {content_len} chars")

        elif event_type == "error":
            print(f"Error: {event.get('message')}")
            break

        elif event_type == "metadata":
            tokens_in = event.get("input_tokens", 0)
            tokens_out = event.get("output_tokens", 0)
            print(f"  [{tokens_in}↓ {tokens_out}↑ tokens]")

    return response


async def main():
    reader, writer = await connect(SOCKET_PATH)

    if len(sys.argv) > 1:
        # One-shot mode: send the command-line argument
        message = " ".join(sys.argv[1:])
        response = await send_message(reader, writer, message)
        print(f"\n{response}")
    else:
        # Interactive REPL
        print("Type a message (Ctrl+C to quit):\n")
        try:
            while True:
                message = input("You: ").strip()
                if not message:
                    continue
                response = await send_message(reader, writer, message)
                print(f"\n{response}\n")
        except (KeyboardInterrupt, EOFError):
            print("\nBye!")

    # Clean up
    await send(writer, {"cmd": "kill", "session": SESSION_NAME})
    writer.close()


if __name__ == "__main__":
    asyncio.run(main())
