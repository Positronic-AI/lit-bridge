#!/usr/bin/env python3
"""lit-monitor: AI CLI session multiplexer.

A lightweight daemon that manages interactive AI CLI sessions in tmux.
Speaks JSON-lines on stdin/stdout. Knows nothing about LIT.

Protocol:
  → {"cmd": "create", "session": "name", "cli": "claude", "parser": "claude-code",
     "args": ["--model", "opus"], "working_dir": "/some/path", "env": {"K": "V"}}
  ← {"session": "name", "event": "ready"}

  → {"cmd": "send", "session": "name", "content": "hello"}
  ← {"session": "name", "event": "state", "from": "idle", "to": "thinking"}
  ← {"session": "name", "event": "chunk", "text": "Let me check..."}
  ← {"session": "name", "event": "state", "from": "responding", "to": "idle"}
  ← {"session": "name", "event": "complete"}

  → {"cmd": "keystroke", "session": "name", "keys": ["Down", "Enter"]}
  → {"cmd": "list"}
  ← {"event": "sessions", "sessions": [...]}
  → {"cmd": "kill", "session": "name"}
  → {"cmd": "ping"}
  ← {"event": "pong"}
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from tmux_session import TmuxSession, sanitize_name
from parsers import SessionState, ClaudeTUIParser
from parsers.base import TUIParser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("lit-monitor")

POLL_INTERVAL = 0.3
STARTUP_WAIT = 3.0
QUIESCENCE_TIMEOUT = 5.0
NO_PROGRESS_TIMEOUT = 30.0

PARSERS = {
    "claude-code": ClaudeTUIParser,
}

CLI_DEFAULTS = {
    "claude": "claude",
}


def find_cli(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise FileNotFoundError(f"CLI not found: {name}")
    return path


def _cc_project_dir(working_dir: str) -> Path:
    """Derive Claude Code's project directory from a working directory path."""
    slug = re.sub(r'[^a-zA-Z0-9]', '-', working_dir.lstrip('/'))
    return Path.home() / ".claude" / "projects" / f"-{slug}"


class JsonlWatcher:
    """Tails a Claude Code JSONL file for tool_use/tool_result events."""

    def __init__(self, project_dir: Path):
        self._project_dir = project_dir
        self._file: Optional[Path] = None
        self._pos: int = 0
        self._emitted_tool_ids: set = set()

    def _find_active_jsonl(self) -> Optional[Path]:
        """Find the most recently modified JSONL in the project dir."""
        if not self._project_dir.is_dir():
            return None
        jsonls = sorted(
            self._project_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return jsonls[0] if jsonls else None

    def begin_turn(self):
        """Call when a new send starts — find/reset the active JSONL."""
        self._file = self._find_active_jsonl()
        if self._file and self._file.exists():
            self._pos = self._file.stat().st_size
        else:
            self._pos = 0
        self._emitted_tool_ids.clear()

    def poll(self) -> List[dict]:
        """Read new JSONL entries and return tool events."""
        if not self._file or not self._file.exists():
            self._file = self._find_active_jsonl()
            if not self._file:
                return []
            self._pos = 0

        try:
            size = self._file.stat().st_size
        except OSError:
            return []

        if size <= self._pos:
            # No new data — check if Claude Code started a new JSONL
            newest = self._find_active_jsonl()
            if newest and newest != self._file:
                self._file = newest
                self._pos = 0
                try:
                    size = self._file.stat().st_size
                except OSError:
                    return []
                if size <= self._pos:
                    return []
            else:
                return []

        events = []
        try:
            with open(self._file, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(self._pos)
                new_data = f.read()
                self._pos = f.tell()
        except OSError:
            return []

        for line in new_data.strip().split('\n'):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get('type') == 'assistant':
                msg = entry.get('message', {})
                for block in msg.get('content', []):
                    if block.get('type') == 'tool_use':
                        tool_id = block.get('id', '')
                        if tool_id not in self._emitted_tool_ids:
                            self._emitted_tool_ids.add(tool_id)
                            events.append({
                                "event": "tool_use",
                                "tool_use_id": tool_id,
                                "name": block.get('name', ''),
                                "input": block.get('input', {}),
                            })
                    elif block.get('type') == 'text':
                        text = block.get('text', '').strip()
                        if text:
                            events.append({
                                "event": "jsonl_text",
                                "text": text,
                            })
            elif entry.get('type') == 'user':
                msg = entry.get('message', {})
                for block in msg.get('content', []):
                    if block.get('type') == 'tool_result':
                        tool_id = block.get('tool_use_id', '')
                        content = block.get('content', '')
                        if isinstance(content, list):
                            text_parts = []
                            for c in content:
                                if isinstance(c, dict) and c.get('type') == 'text':
                                    text_parts.append(c.get('text', ''))
                                elif isinstance(c, str):
                                    text_parts.append(c)
                            content = '\n'.join(text_parts)
                        if len(str(content)) > 5000:
                            content = str(content)[:5000] + "…"
                        events.append({
                            "event": "tool_result",
                            "tool_use_id": tool_id,
                            "content": content,
                        })

        if events:
            log.info(f"JSONL watcher: {len(events)} tool events from {self._file.name}")
        return events


class ManagedSession:
    """A tmux session + its parser + observer state."""

    def __init__(self, name: str, tmux: TmuxSession, parser: TUIParser,
                 working_dir: str = None):
        self.name = name
        self.tmux = tmux
        self.parser = parser
        self.state = SessionState.STARTING
        self.observing = False
        self._observe_task: Optional[asyncio.Task] = None
        self._jsonl_watcher: Optional[JsonlWatcher] = None
        if working_dir:
            project_dir = _cc_project_dir(working_dir)
            self._jsonl_watcher = JsonlWatcher(project_dir)
            log.info(f"[{name}] JSONL watcher: {project_dir}")


class Monitor:

    def __init__(self):
        self.sessions: Dict[str, ManagedSession] = {}
        self._running = True

    # ── Output ──────────────────────────────────────────────

    def _emit(self, event: dict):
        line = json.dumps(event, ensure_ascii=False)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def _emit_error(self, session: str, message: str):
        self._emit({"session": session, "event": "error", "message": message})

    # ── Command dispatch ────────────────────────────────────

    async def handle_command(self, cmd: dict):
        action = cmd.get("cmd")
        if action == "create":
            await self._cmd_create(cmd)
        elif action == "send":
            await self._cmd_send(cmd)
        elif action == "input":
            await self._cmd_input(cmd)
        elif action == "keystroke":
            await self._cmd_keystroke(cmd)
        elif action == "kill":
            await self._cmd_kill(cmd)
        elif action == "list":
            await self._cmd_list()
        elif action == "status":
            await self._cmd_status(cmd)
        elif action == "ping":
            self._emit({"event": "pong"})
        else:
            self._emit({"event": "error", "message": f"unknown command: {action}"})

    # ── Commands ────────────────────────────────────────────

    async def _cmd_create(self, cmd: dict):
        name = cmd.get("session")
        if not name:
            self._emit({"event": "error", "message": "session name required"})
            return

        if name in self.sessions:
            ms = self.sessions[name]
            if await ms.tmux.is_alive():
                self._emit({"session": name, "event": "ready", "reused": True})
                return
            del self.sessions[name]

        parser_name = cmd.get("parser", "claude-code")
        parser_cls = PARSERS.get(parser_name)
        if not parser_cls:
            self._emit_error(name, f"unknown parser: {parser_name}")
            return

        cli_name = cmd.get("cli", "claude")
        try:
            cli_path = find_cli(CLI_DEFAULTS.get(cli_name, cli_name))
        except FileNotFoundError as e:
            self._emit_error(name, str(e))
            return

        cli_args = cmd.get("args", [])
        working_dir = cmd.get("working_dir")
        env_vars = cmd.get("env", {})

        full_cmd = [cli_path] + cli_args

        tmux = TmuxSession(sanitize_name(name))
        parser = parser_cls()

        log.info(f"Creating session '{name}': {' '.join(full_cmd)}")

        try:
            await tmux.spawn(full_cmd, env_vars, working_dir)
        except RuntimeError as e:
            self._emit_error(name, f"spawn failed: {e}")
            return

        ms = ManagedSession(name, tmux, parser, working_dir=working_dir)
        self.sessions[name] = ms

        await asyncio.sleep(STARTUP_WAIT)

        # Auto-dismiss startup dialogs
        for attempt in range(5):
            if not await tmux.is_alive():
                self._emit_error(name, "session died during startup")
                del self.sessions[name]
                return

            capture = await tmux.capture_pane(visible_only=True)
            dialog = parser.is_startup_dialog(capture)
            if not dialog:
                break

            _, keys, dialog_name = dialog
            log.info(f"Auto-dismissing dialog: {dialog_name}")
            for key in keys:
                await tmux.send_keys(key)
                await asyncio.sleep(0.3)
            await asyncio.sleep(2.0)

        if not await tmux.is_alive():
            self._emit_error(name, "session died after startup")
            del self.sessions[name]
            return

        capture = await tmux.capture_pane(visible_only=True)
        ms.state = parser.detect_state(capture)

        self._emit({"session": name, "event": "ready", "state": ms.state.value})

        # Start background observer
        ms._observe_task = asyncio.create_task(self._observe_loop(ms))

    async def _cmd_send(self, cmd: dict):
        name = cmd.get("session")
        content = cmd.get("content", "")

        ms = self.sessions.get(name)
        if not ms:
            self._emit_error(name, "session not found")
            return

        if not await ms.tmux.is_alive():
            ms.state = SessionState.DEAD
            self._emit({"session": name, "event": "state",
                         "from": ms.state.value, "to": "dead"})
            return

        visible = await ms.tmux.capture_pane(visible_only=True)
        current_state = ms.parser.detect_state(visible)

        if current_state == SessionState.DIALOG:
            self._emit({"session": name, "event": "error",
                         "message": "session is in dialog state, use keystroke command"})
            return

        if current_state not in (SessionState.IDLE, SessionState.THINKING):
            self._emit({"session": name, "event": "error",
                         "message": f"session is {current_state.value}, not ready"})
            return

        full_capture = await ms.tmux.capture_pane()
        baseline_count = ms.parser.count_assistant_messages(full_capture)

        if ms._jsonl_watcher:
            ms._jsonl_watcher.begin_turn()

        log.info(f"[{name}] Sending {len(content)} chars (baseline={baseline_count})")

        try:
            await ms.tmux.send_message(content)
        except RuntimeError as e:
            self._emit_error(name, f"send failed: {e}")
            return

        old_state = ms.state
        ms.state = SessionState.THINKING
        self._emit({"session": name, "event": "state",
                     "from": old_state.value, "to": "thinking"})

        # Response observation is handled by _observe_loop
        # Store baseline so the observer knows when new content appears
        ms._baseline_count = baseline_count
        ms._yielded = ""
        ms.observing = True

    async def _cmd_input(self, cmd: dict):
        """Send text to session without triggering observation.

        Used for slash commands like /model, /effort that produce
        brief output but shouldn't be treated as a response turn.
        """
        name = cmd.get("session")
        content = cmd.get("content", "")

        ms = self.sessions.get(name)
        if not ms:
            self._emit_error(name, "session not found")
            return

        try:
            await ms.tmux.send_message(content)
            self._emit({"session": name, "event": "input_sent"})
        except RuntimeError as e:
            self._emit_error(name, f"input failed: {e}")

    async def _cmd_keystroke(self, cmd: dict):
        name = cmd.get("session")
        keys = cmd.get("keys", [])

        ms = self.sessions.get(name)
        if not ms:
            self._emit_error(name, "session not found")
            return

        for key in keys:
            await ms.tmux.send_keys(key)
            await asyncio.sleep(0.1)

        self._emit({"session": name, "event": "keystroke_sent", "keys": keys})

    async def _cmd_kill(self, cmd: dict):
        name = cmd.get("session")
        ms = self.sessions.get(name)
        if not ms:
            self._emit_error(name, "session not found")
            return

        if ms._observe_task:
            ms._observe_task.cancel()
        await ms.tmux.kill()
        del self.sessions[name]
        self._emit({"session": name, "event": "killed"})

    async def _cmd_list(self):
        sessions = []
        for name, ms in self.sessions.items():
            visible = await ms.tmux.capture_pane(visible_only=True)
            live_state = ms.parser.detect_state(visible) if visible.strip() else ms.state
            ms.state = live_state
            sessions.append({
                "name": name,
                "state": live_state.value,
                "alive": ms.tmux._alive,
                "message_count": ms.tmux.message_count,
            })
        self._emit({"event": "sessions", "sessions": sessions})

    async def _cmd_status(self, cmd: dict):
        name = cmd.get("session")
        ms = self.sessions.get(name)
        if not ms:
            self._emit_error(name, "session not found")
            return

        visible = await ms.tmux.capture_pane(visible_only=True)
        state = ms.parser.detect_state(visible)
        full_capture = await ms.tmux.capture_pane()
        tui = ms.parser.parse(full_capture)

        self._emit({
            "session": name,
            "event": "status",
            "state": state.value,
            "message_count": len(tui.messages),
            "version": tui.version,
            "errors": tui.errors,
        })

    # ── Background observer ─────────────────────────────────

    async def _observe_loop(self, ms: ManagedSession):
        """Continuous TUI observation for a session.

        Emits state changes and response chunks as events.
        """
        prev_state = ms.state
        last_response_change = time.monotonic()
        last_capture_change = time.monotonic()
        prev_capture = ""

        while self._running:
            try:
                await asyncio.sleep(POLL_INTERVAL)

                if not await ms.tmux.is_alive():
                    if ms.state != SessionState.DEAD:
                        self._emit({"session": ms.name, "event": "state",
                                     "from": ms.state.value, "to": "dead"})
                        ms.state = SessionState.DEAD
                    break

                visible = await ms.tmux.capture_pane(visible_only=True)
                now = time.monotonic()

                if visible != prev_capture:
                    last_capture_change = now
                    prev_capture = visible

                new_state = ms.parser.detect_state(visible)

                # Emit state transitions
                if new_state != prev_state:
                    self._emit({"session": ms.name, "event": "state",
                                 "from": prev_state.value, "to": new_state.value})
                    prev_state = new_state
                    ms.state = new_state

                # Emit response chunks when actively observing a send
                if ms.observing and hasattr(ms, '_baseline_count'):
                    full_capture = await ms.tmux.capture_pane()
                    response = ms.parser.extract_new_response(
                        ms._baseline_count, full_capture)

                    if len(response) > len(ms._yielded):
                        new_text = response[len(ms._yielded):]
                        self._emit({"session": ms.name, "event": "chunk",
                                     "text": new_text})
                        ms._yielded = response
                        last_response_change = now

                    # Emit structured tool events from JSONL
                    if ms._jsonl_watcher:
                        for tool_evt in ms._jsonl_watcher.poll():
                            tool_evt["session"] = ms.name
                            self._emit(tool_evt)
                            last_response_change = now

                    # Completion: back to idle with content
                    if ms._yielded and new_state == SessionState.IDLE:
                        self._emit({"session": ms.name, "event": "complete",
                                     "total_length": len(ms._yielded)})
                        ms.observing = False
                        log.info(f"[{ms.name}] Response complete ({len(ms._yielded)} chars)")

                    # Quiescence: response stopped growing
                    if (ms._yielded and
                            (now - last_response_change) > QUIESCENCE_TIMEOUT and
                            new_state in (SessionState.IDLE, SessionState.RESPONDING)):
                        self._emit({"session": ms.name, "event": "complete",
                                     "total_length": len(ms._yielded),
                                     "reason": "quiescence"})
                        ms.observing = False
                        log.info(f"[{ms.name}] Response complete (quiescence)")

                    # No progress timeout
                    if (not ms._yielded and
                            (now - last_capture_change) > NO_PROGRESS_TIMEOUT):
                        self._emit({"session": ms.name, "event": "error",
                                     "message": "no progress timeout"})
                        ms.observing = False

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[{ms.name}] Observer error: {e}", exc_info=True)
                await asyncio.sleep(1.0)

    # ── Main loop ───────────────────────────────────────────

    async def run(self):
        log.info("lit-monitor starting")

        # Discover existing lit-* tmux sessions
        await self._discover_existing()

        self._emit({"event": "monitor_ready",
                     "sessions": len(self.sessions)})

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(
            lambda: protocol, sys.stdin
        )

        while self._running:
            try:
                line = await reader.readline()
                if not line:
                    log.info("stdin closed, shutting down")
                    break

                line = line.decode('utf-8', errors='replace').strip()
                if not line:
                    continue

                try:
                    cmd = json.loads(line)
                except json.JSONDecodeError as e:
                    self._emit({"event": "error",
                                 "message": f"invalid JSON: {e}"})
                    continue

                await self.handle_command(cmd)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Command loop error: {e}", exc_info=True)

        await self._shutdown()

    async def _discover_existing(self):
        """Find running lit-* tmux sessions on startup."""
        try:
            proc = await asyncio.create_subprocess_shell(
                "tmux ls 2>/dev/null || true",
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode('utf-8', errors='replace')
        except Exception as e:
            log.debug(f"Session discovery failed: {e}")
            return

        for line in output.strip().split('\n'):
            if not line or ':' not in line:
                continue
            session_name = line.split(':')[0].strip()
            if not session_name.startswith('lit-'):
                continue

            tmux = TmuxSession(session_name)
            tmux._alive = True
            parser = ClaudeTUIParser()

            visible = await tmux.capture_pane(visible_only=True)
            if not visible.strip():
                continue

            ms = ManagedSession(session_name, tmux, parser)
            ms.state = parser.detect_state(visible)
            ms._observe_task = asyncio.create_task(self._observe_loop(ms))
            self.sessions[session_name] = ms

            log.info(f"Discovered existing session: {session_name} "
                     f"(state={ms.state.value})")

    async def _shutdown(self):
        log.info(f"Shutting down ({len(self.sessions)} sessions still running)")
        self._running = False
        for ms in self.sessions.values():
            if ms._observe_task:
                ms._observe_task.cancel()
        self._emit({"event": "monitor_shutdown"})


def main():
    monitor = Monitor()
    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
