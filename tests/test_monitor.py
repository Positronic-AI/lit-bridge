#!/usr/bin/env python3
"""Integration test for lit-bridge.

Spawns monitor as a subprocess, sends commands via stdin,
reads events from stdout. No LIT dependencies.
"""

import asyncio
import json
import sys
import os

MONITOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def read_event(proc, timeout=30.0):
    """Read one JSON event from the monitor's stdout."""
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
    if not line:
        return None
    return json.loads(line.decode('utf-8').strip())


async def send_cmd(proc, cmd):
    """Send a JSON command to the monitor's stdin."""
    line = json.dumps(cmd) + "\n"
    proc.stdin.write(line.encode('utf-8'))
    await proc.stdin.drain()


async def test_ping():
    """Test: ping → pong."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, os.path.join(MONITOR_DIR, "server.py"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=MONITOR_DIR,
    )

    try:
        ready = await read_event(proc, timeout=5)
        assert ready["event"] == "monitor_ready", f"expected monitor_ready, got {ready}"

        await send_cmd(proc, {"cmd": "ping"})
        pong = await read_event(proc, timeout=5)
        assert pong["event"] == "pong", f"expected pong, got {pong}"

        await send_cmd(proc, {"cmd": "list"})
        listing = await read_event(proc, timeout=5)
        assert listing["event"] == "sessions", f"expected sessions, got {listing}"
        assert listing["sessions"] == [], f"expected empty sessions"

        print("PASS: test_ping")
    finally:
        proc.stdin.close()
        await proc.wait()


async def test_create_and_send():
    """Test: create a Claude session, send a message, get chunks back."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, os.path.join(MONITOR_DIR, "server.py"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=MONITOR_DIR,
    )

    session_name = "test-integration"

    try:
        ready = await read_event(proc, timeout=5)
        assert ready["event"] == "monitor_ready"

        # Create session
        await send_cmd(proc, {
            "cmd": "create",
            "session": session_name,
            "cli": "claude",
            "parser": "claude-code",
            "args": ["--model", "haiku", "--dangerously-skip-permissions"],
            "working_dir": "/tmp",
        })

        # Wait for ready (may take a while with dialog dismissal)
        event = await read_event(proc, timeout=30)
        assert event["event"] == "ready", f"expected ready, got {event}"
        assert event["session"] == session_name
        print(f"  Session ready (state={event.get('state')})")

        # Send a simple message
        await send_cmd(proc, {
            "cmd": "send",
            "session": session_name,
            "content": "Reply with exactly: MONITOR_TEST_OK",
        })

        # Read events until we get a complete
        chunks = []
        got_thinking = False
        got_complete = False

        while True:
            event = await read_event(proc, timeout=60)
            if not event:
                break

            if event.get("event") == "state":
                print(f"  State: {event.get('from')} → {event.get('to')}")
                if event.get("to") == "thinking":
                    got_thinking = True

            elif event.get("event") == "chunk":
                chunks.append(event["text"])
                sys.stdout.write(".")
                sys.stdout.flush()

            elif event.get("event") == "complete":
                got_complete = True
                print(f"\n  Complete ({event.get('total_length')} chars)")
                break

            elif event.get("event") == "error":
                print(f"\n  Error: {event.get('message')}")
                break

        full_response = "".join(chunks)
        print(f"  Response: {full_response[:200]}")

        assert got_complete, "never got complete event"
        assert len(full_response) > 0, "empty response"

        # Kill session
        await send_cmd(proc, {"cmd": "kill", "session": session_name})
        event = await read_event(proc, timeout=5)
        assert event["event"] == "killed"

        print("PASS: test_create_and_send")

    except Exception as e:
        print(f"FAIL: test_create_and_send — {e}")
        # Clean up tmux session if it exists
        cleanup = await asyncio.create_subprocess_shell(
            f"tmux kill-session -t {session_name} 2>/dev/null || true"
        )
        await cleanup.wait()
        raise
    finally:
        proc.stdin.close()
        await proc.wait()


async def main():
    print("=== lit-bridge integration tests ===\n")

    print("--- test_ping ---")
    await test_ping()

    print("\n--- test_create_and_send ---")
    await test_create_and_send()

    print("\n=== All tests passed ===")


if __name__ == "__main__":
    asyncio.run(main())
