#!/usr/bin/env python3
"""lit-bridge: AI CLI session multiplexer.

A lightweight daemon that manages interactive AI CLI sessions in tmux.
Speaks JSON-lines protocol. Knows nothing about LIT.

Modes:
  stdio:   reads stdin, writes stdout (legacy, dies with parent)
  socket:  listens on a Unix domain socket (daemon, survives API restarts)

Usage:
  python3 monitor.py                          # stdio mode
  python3 monitor.py --socket /tmp/lit-bridge-ben.sock   # socket mode

Protocol (same in both modes):
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

import argparse
import asyncio
import collections
import json
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from tmux_session import TmuxSession, sanitize_name
from parsers import SessionState
from parsers.base import TUIParser
from parsers.registry import select_parser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("lit-bridge")

POLL_INTERVAL = 0.3
STARTUP_WAIT = 3.0
QUIESCENCE_TIMEOUT = 5.0
QUIESCENCE_UNCONFIRMED_TIMEOUT = 30.0
COMPLETION_DEBOUNCE = 2.0  # require stable IDLE+confirmed for 2s before firing
AUTO_OBSERVE_COOLDOWN = 1.5
NO_PROGRESS_TIMEOUT = 90.0
IDLE_REAP_TIMEOUT = 3600.0  # 1 hour idle → kill session, resumable
EVENT_BUFFER_MAX = 500


CLI_DEFAULTS = {
    "claude": "claude",
}


def _parse_compact_pct(capture: str) -> Optional[int]:
    """Extract the auto-compact percentage from visible capture."""
    for line in capture.split('\n'):
        m = re.match(r'^\s*(\d+)%\s+until\s+auto-compact', line.strip())
        if m:
            return int(m.group(1))
    return None


def _unwrap_tmux_lines(text: str, pane_width: int) -> str:
    """Remove hard line breaks from tmux captures.

    Two kinds of wrapping:
    1. Tmux hard wrap — line is exactly pane_width chars.  Join directly.
    2. TUI word wrap — the CLI word-wraps within its ●/continuation block.
       The previous line is near-full (>= pane_width - 20) and the next
       line starts with the 2-space continuation indent.  Join with space.
    Structural continuations (list items, code fences, tool output) are
    preserved.
    """
    if not text or pane_width <= 0:
        return text
    threshold = pane_width - 20
    lines = text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        while i + 1 < len(lines) and lines[i + 1]:
            next_line = lines[i + 1]
            if (len(lines[i]) >= threshold
                    and next_line.startswith('  ')
                    and len(next_line) > 2
                    and next_line[2] not in '-*>⎿#`│┌└├'
                    and not (next_line[2].isdigit() and '.' in next_line[2:5])):
                i += 1
                line = line.rstrip() + ' ' + lines[i].lstrip()
            elif len(lines[i]) == pane_width:
                i += 1
                line += lines[i]
            else:
                break
        result.append(line)
        i += 1
    return '\n'.join(result)


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
                content_blocks = msg.get('content', [])
                if not isinstance(content_blocks, list):
                    continue
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
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
                content_blocks = msg.get('content', [])
                if not isinstance(content_blocks, list):
                    continue
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
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

    def get_turn_metadata(self) -> Optional[dict]:
        """Aggregate usage stats from the most recent turn's JSONL entries.

        Reads backwards from the end to find all assistant messages in the
        current turn (until we hit a user message that isn't a tool_result).
        Sums token counts across multi-step tool-use turns.
        """
        f = self._file or self._find_active_jsonl()
        if not f or not f.exists():
            return None
        try:
            lines = f.read_text(encoding='utf-8', errors='replace').strip().split('\n')
        except OSError:
            return None

        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_create = 0
        num_steps = 0
        model = None

        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get('type') == 'assistant':
                msg = entry.get('message', {})
                usage = msg.get('usage', {})
                if usage:
                    total_input += usage.get('input_tokens', 0)
                    total_output += usage.get('output_tokens', 0)
                    total_cache_read += usage.get('cache_read_input_tokens', 0)
                    total_cache_create += usage.get('cache_creation_input_tokens', 0)
                    num_steps += 1
                    if not model:
                        model = msg.get('model')
            elif entry.get('type') == 'user':
                msg = entry.get('message', {})
                content = msg.get('content', [])
                if isinstance(content, list):
                    has_tool_result = any(
                        isinstance(b, dict) and b.get('type') == 'tool_result'
                        for b in content
                    )
                    if has_tool_result:
                        continue
                break
            elif entry.get('type') in ('system', 'attachment', 'mode'):
                continue
            else:
                break

        if num_steps == 0:
            return None

        return {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_tokens": total_cache_read,
            "cache_create_tokens": total_cache_create,
            "num_steps": num_steps,
            "model": model,
        }

    def get_session_id(self) -> Optional[str]:
        """Get the Claude Code session ID from the active JSONL filename."""
        f = self._file or self._find_active_jsonl()
        if not f:
            return None
        return f.stem  # filename without .jsonl extension

    def get_last_user_message(self) -> Optional[str]:
        """Read the JSONL backwards to find the most recent user text message."""
        f = self._file or self._find_active_jsonl()
        if not f or not f.exists():
            log.info(f"get_last_user_message: no file (file={self._file})")
            return None
        log.info(f"get_last_user_message: reading {f.name} ({f.stat().st_size} bytes)")
        try:
            lines = f.read_text(encoding='utf-8', errors='replace').strip().split('\n')
        except OSError as e:
            log.info(f"get_last_user_message: OSError {e}")
            return None
        user_count = 0
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get('type') not in ('human', 'user'):
                continue
            user_count += 1
            msg = entry.get('message', {})
            content = msg.get('content', '')
            if isinstance(content, str) and content.strip():
                log.info(f"get_last_user_message: found at user#{user_count}: {content.strip()[:80]!r}")
                return content.strip()
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        text = block.get('text', '').strip()
                        if text:
                            log.info(f"get_last_user_message: found block at user#{user_count}: {text[:80]!r}")
                            return text
            if user_count >= 3:
                break
        log.info(f"get_last_user_message: no text user message found in {user_count} user entries")
        return None


class ManagedSession:
    """A tmux session + its parser + observer state."""

    def __init__(self, name: str, tmux: TmuxSession, parser: TUIParser,
                 working_dir: str = None, channel_id: str = None,
                 team: str = None):
        self.name = name
        self.tmux = tmux
        self.parser = parser
        self.working_dir = working_dir
        self.channel_id = channel_id
        self.team = team
        self.state = SessionState.STARTING
        self.observing = False
        self._is_organic = False
        self._observe_task: Optional[asyncio.Task] = None
        self._jsonl_watcher: Optional[JsonlWatcher] = None
        self._claude_session_id: Optional[str] = None
        self._last_active: float = time.monotonic()
        if working_dir:
            project_dir = _cc_project_dir(working_dir)
            self._jsonl_watcher = JsonlWatcher(project_dir)
            log.info(f"[{name}] JSONL watcher: {project_dir}")


class Monitor:

    def __init__(self, socket_path: str = None):
        self.sessions: Dict[str, ManagedSession] = {}
        self._reaped_sessions: Dict[str, dict] = {}  # name → {session_id, working_dir, args, env}
        self._running = True
        self._socket_path = socket_path
        self._client_writer: Optional[asyncio.StreamWriter] = None
        self._event_buffer: collections.deque = collections.deque(maxlen=EVENT_BUFFER_MAX)

    # ── Output ──────────────────────────────────────────────

    def _emit(self, event: dict):
        line = json.dumps(event, ensure_ascii=False) + "\n"
        if self._client_writer and not self._client_writer.is_closing():
            try:
                self._client_writer.write(line.encode('utf-8'))
            except (ConnectionError, RuntimeError):
                self._client_writer = None
                self._event_buffer.append(line)
        elif self._socket_path:
            self._event_buffer.append(line)
        else:
            sys.stdout.write(line)
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

    def _session_key(self, name: str, channel_id: str = None) -> str:
        return f"{name}:{channel_id}" if channel_id else name

    def _find_any_session_for(self, name: str) -> Optional['ManagedSession']:
        """Find any existing ManagedSession whose tmux session matches name."""
        for key, ms in self.sessions.items():
            if key == name or key.startswith(f"{name}:"):
                return ms
        return None

    async def _cmd_create(self, cmd: dict):
        name = cmd.get("session")
        if not name:
            self._emit({"event": "error", "message": "session name required"})
            return

        channel_id = cmd.get("channel_id")
        team = cmd.get("team")
        session_key = self._session_key(name, channel_id)

        # Exact match — reuse existing window
        if session_key in self.sessions:
            ms = self.sessions[session_key]
            if await ms.tmux.is_alive():
                self._emit({"session": session_key, "event": "ready", "reused": True})
                return
            del self.sessions[session_key]

        parser_name = cmd.get("parser", "claude-code")
        parser = select_parser(parser_name)
        if not parser:
            self._emit_error(session_key, f"unknown parser: {parser_name}")
            return

        cli_name = cmd.get("cli", "claude")
        try:
            cli_path = find_cli(CLI_DEFAULTS.get(cli_name, cli_name))
        except FileNotFoundError as e:
            self._emit_error(session_key, str(e))
            return

        cli_args = list(cmd.get("args", []))
        working_dir = cmd.get("working_dir")
        env_vars = cmd.get("env", {})

        # Resume from a previously reaped idle session
        reaped = self._reaped_sessions.pop(session_key, None)
        if reaped and reaped.get("session_id"):
            cli_args.extend(["--resume", reaped["session_id"]])
            log.info(f"[{session_key}] Resuming from reaped session {reaped['session_id']}")

        full_cmd = [cli_path] + cli_args
        tmux_session_name = sanitize_name(name)
        win_label = f"{team}:{channel_id}" if (team and channel_id) else (channel_id or None)
        window_name = sanitize_name(channel_id) if channel_id else None

        # Adopt orphaned tmux window (survives monitor restarts)
        # Windows are renamed to win_label after creation, so check that first
        orphan_tmux = TmuxSession(tmux_session_name, window_name=win_label or window_name)
        if await orphan_tmux.is_alive():
            log.info(f"[{session_key}] Adopting orphaned tmux window '{orphan_tmux.window_name}' in '{tmux_session_name}'")
            await orphan_tmux._exec(f"tmux set-option -t {shlex.quote(tmux_session_name)} history-limit 50000")
            # Evict any discovered session using the bare tmux name — startup
            # discovery registers under `name` but the heartbeat creates under
            # `name:channel_id`.  Without this, two observe loops watch the
            # same pane and the organic relay fires duplicates.
            if name in self.sessions and name != session_key:
                old = self.sessions.pop(name)
                if old._observe_task:
                    old._observe_task.cancel()
                log.info(f"[{session_key}] Evicted discovered session '{name}' (superseded)")
            ms = ManagedSession(session_key, orphan_tmux, parser, working_dir=working_dir,
                                channel_id=channel_id, team=team)
            self.sessions[session_key] = ms
            capture = await orphan_tmux.capture_pane(visible_only=True)
            ms.state = parser.detect_state(capture)
            ms._observe_task = asyncio.create_task(self._observe_loop(ms))
            self._emit({"session": session_key, "event": "ready", "reused": True})
            return

        # Check if the tmux session already exists (in-memory or directly)
        existing = self._find_any_session_for(name)
        session_probe = TmuxSession(tmux_session_name)
        tmux_session_alive = (existing and await existing.tmux.session_exists()) or await session_probe.session_exists()

        if tmux_session_alive:
            tmux = TmuxSession(tmux_session_name, window_name=window_name)
            log.info(f"Creating window '{window_name}' in session '{tmux_session_name}': {' '.join(full_cmd)}")
            try:
                await tmux.create_window(full_cmd, env_vars, working_dir)
            except RuntimeError as e:
                self._emit_error(session_key, f"create_window failed: {e}")
                return
        else:
            tmux = TmuxSession(tmux_session_name, window_name=window_name)
            log.info(f"Creating session '{tmux_session_name}': {' '.join(full_cmd)}")
            try:
                await tmux.spawn(full_cmd, env_vars, working_dir)
            except RuntimeError as e:
                self._emit_error(session_key, f"spawn failed: {e}")
                return

        ms = ManagedSession(session_key, tmux, parser, working_dir=working_dir,
                            channel_id=channel_id, team=team)
        self.sessions[session_key] = ms

        if win_label:
            await tmux.rename_window(win_label)
            tmux.window_name = win_label

        await asyncio.sleep(STARTUP_WAIT)

        # Auto-dismiss startup dialogs
        for attempt in range(5):
            if not await tmux.is_alive():
                self._emit_error(session_key, "session died during startup")
                await tmux.kill()
                del self.sessions[session_key]
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
            self._emit_error(session_key, "session died after startup")
            await tmux.kill()
            del self.sessions[session_key]
            return

        capture = await tmux.capture_pane(visible_only=True)
        ms.state = parser.detect_state(capture)

        self._emit({"session": session_key, "event": "ready", "state": ms.state.value})

        # Start background observer
        ms._observe_task = asyncio.create_task(self._observe_loop(ms))

    async def _cmd_send(self, cmd: dict):
        name = cmd.get("session")
        channel_id = cmd.get("channel_id")
        session_key = self._session_key(name, channel_id)
        content = cmd.get("content", "")

        ms = self.sessions.get(session_key)
        if not ms and channel_id:
            ms = self.sessions.get(name)
        if not ms:
            self._emit_error(session_key, "session not found")
            return

        ms._last_active = time.monotonic()
        sk = ms.name  # use the key stored on the session for all events

        if not await ms.tmux.is_alive():
            ms.state = SessionState.DEAD
            self._emit({"session": sk, "event": "state",
                         "from": ms.state.value, "to": "dead"})
            return

        # Wait for session to become ready (handles startup rendering + organic responses)
        for attempt in range(20):
            visible = await ms.tmux.capture_pane(visible_only=True)
            current_state = ms.parser.detect_state(visible)

            if current_state == SessionState.DIALOG:
                self._emit({"session": sk, "event": "error",
                             "message": "session is in dialog state, use keystroke command"})
                return

            if current_state in (SessionState.IDLE, SessionState.THINKING):
                break

            if attempt == 0:
                log.info(f"[{sk}] Waiting for idle (currently {current_state.value})")
            await asyncio.sleep(0.5)
        else:
            self._emit({"session": sk, "event": "error",
                         "message": f"session is {current_state.value}, not ready after 10s"})
            return

        full_capture = await ms.tmux.capture_pane()
        baseline_count = ms.parser.count_assistant_messages(full_capture)
        ms._pane_width = await ms.tmux.get_pane_width()

        if ms._jsonl_watcher:
            ms._jsonl_watcher.begin_turn()

        log.info(f"[{sk}] Sending {len(content)} chars (baseline={baseline_count}, pane_width={ms._pane_width})")

        # Set observation state BEFORE sending to tmux to prevent the
        # observe loop from racing and marking this as organic
        ms._baseline_count = baseline_count
        ms._sent_content = content
        ms._yielded = ""
        blocks = ms.parser.extract_content_blocks(full_capture)
        ms._baseline_tool_count = len(
            [b for b in blocks if b.type in ("tool_call", "tool_output")])
        ms._emitted_tool_count = 0
        ms._streaming_tool_output = False
        ms._last_tool_output_content = ""
        ms._is_organic = False
        ms._compact_pct_start = _parse_compact_pct(visible)
        ms.observing = True

        try:
            await ms.tmux.send_message(content)
        except RuntimeError as e:
            ms.observing = False
            self._emit_error(sk, f"send failed: {e}")
            return

        old_state = ms.state
        ms.state = SessionState.THINKING
        self._emit({"session": sk, "event": "state",
                     "from": old_state.value, "to": "thinking"})

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
        last_observe_complete = time.monotonic()
        idle_confirmed_since = 0.0  # when IDLE+confirmed was first seen
        prev_capture = ""
        poll_count = 0

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
                poll_count += 1

                if poll_count % 100 == 0:
                    log.info(f"[{ms.name}] heartbeat poll={poll_count} "
                             f"state={prev_state.value} observing={ms.observing} "
                             f"client={'yes' if self._client_writer else 'no'}")

                if visible != prev_capture:
                    last_capture_change = now
                    prev_capture = visible

                new_state = ms.parser.detect_state(visible)
                transitioned_from_idle = (
                    new_state != prev_state and
                    prev_state == SessionState.IDLE
                )

                # Verbose logging when not observing: detect missed transitions
                if not ms.observing and poll_count % 10 == 0:
                    last_lines = visible.strip().split('\n')[-3:] if visible else []
                    log.info(f"[{ms.name}] poll state={new_state.value} "
                             f"bottom={[l.strip()[:60] for l in last_lines]}")

                # Log state changes for diagnostics
                if new_state != prev_state:
                    log.info(f"[{ms.name}] State: {prev_state.value} → {new_state.value} "
                             f"(observing={ms.observing})")

                # Emit state transitions
                if new_state != prev_state:
                    self._emit({"session": ms.name, "event": "state",
                                 "from": prev_state.value, "to": new_state.value})
                    prev_state = new_state
                    ms.state = new_state

                # Track last active time for idle reaping
                if new_state != SessionState.IDLE or ms.observing:
                    ms._last_active = now

                # Idle reaping: kill sessions idle too long, store for --resume
                if (not ms.observing and
                        new_state == SessionState.IDLE and
                        (now - ms._last_active) > IDLE_REAP_TIMEOUT):
                    session_id = None
                    if ms._jsonl_watcher:
                        session_id = ms._jsonl_watcher.get_session_id()
                    self._reaped_sessions[ms.name] = {
                        "session_id": session_id,
                        "working_dir": ms.working_dir,
                    }
                    log.info(
                        f"[{ms.name}] Reaping idle session "
                        f"(idle {now - ms._last_active:.0f}s, "
                        f"resume_id={session_id})"
                    )
                    await ms.tmux.kill()
                    self._emit({"session": ms.name, "event": "reaped",
                                 "resume_session_id": session_id})
                    del self.sessions[ms.name]
                    break

                # Auto-observe: detect organic interaction (direct tmux typing).
                # Only triggers on a fresh IDLE→active transition — prevents
                # re-observe loops when residual TUI state looks non-idle after
                # a quiescence completion.
                if (not ms.observing and
                        transitioned_from_idle and
                        new_state in (SessionState.THINKING, SessionState.RESPONDING) and
                        (now - last_observe_complete) > AUTO_OBSERVE_COOLDOWN):
                    full_capture = await ms.tmux.capture_pane()
                    ms._baseline_count = ms.parser.count_assistant_messages(full_capture)
                    ms._pane_width = await ms.tmux.get_pane_width()
                    ms._yielded = ""
                    blocks = ms.parser.extract_content_blocks(full_capture)
                    ms._baseline_tool_count = len(
                        [b for b in blocks
                         if b.type in ("tool_call", "tool_output")])
                    ms._emitted_tool_count = 0
                    ms._streaming_tool_output = False
                    ms._last_tool_output_content = ""
                    if ms._jsonl_watcher:
                        user_msg = ms._jsonl_watcher.get_last_user_message()
                        log.info(f"[{ms.name}] Organic user_input lookup: {user_msg!r}")
                        if not user_msg:
                            log.info(f"[{ms.name}] Skipping auto-observe — no user message in JSONL")
                            continue
                        evt = {"session": ms.name, "event": "user_input",
                               "text": user_msg, "organic": True}
                        if ms.channel_id:
                            evt["channel_id"] = ms.channel_id
                        if ms.team:
                            evt["team"] = ms.team
                        self._emit(evt)
                        ms._jsonl_watcher.begin_turn()
                    else:
                        log.info(f"[{ms.name}] No JSONL watcher for organic input")
                    ms._is_organic = True
                    ms.observing = True
                    log.info(f"[{ms.name}] Auto-observing organic interaction")

                # Emit response content when actively observing
                # Full/replace model: emit the complete current response
                # each poll.  The frontend replaces (not appends) its
                # display, so the content is always the current truth.
                if ms.observing and hasattr(ms, '_baseline_count'):
                    full_capture = await ms.tmux.capture_pane()
                    response = ms.parser.extract_raw_response(
                        ms._baseline_count, full_capture,
                        sent_content=getattr(ms, '_sent_content', None))
                    response = _unwrap_tmux_lines(response, getattr(ms, '_pane_width', 0))

                    # Detect definitive turn completion: a line-start ✻
                    # marker (e.g. "✻ Brewed for 9s") after the last
                    # line-start ● block.  Must match at line start to
                    # avoid false positives when response prose contains
                    # these Unicode characters mid-text.
                    turn_confirmed = False
                    last_bullet_line = -1
                    for li, ln in enumerate(full_capture.split('\n')):
                        if re.match(r'^\s*●\s', ln):
                            last_bullet_line = li
                    if last_bullet_line >= 0:
                        after_lines = full_capture.split('\n')[last_bullet_line:]
                        for ln in after_lines:
                            if re.match(r'^\s*✻\s', ln):
                                turn_confirmed = not re.search(r'✻\s+.*…', ln)
                                break

                    if response != ms._yielded:
                        evt = {"session": ms.name, "event": "replace",
                               "text": response,
                               "organic": ms._is_organic}
                        if ms.channel_id:
                            evt["channel_id"] = ms.channel_id
                        if ms.team:
                            evt["team"] = ms.team
                        self._emit(evt)
                        ms._yielded = response
                        last_response_change = now

                    # JSONL watcher: drain events, keep metadata
                    if ms._jsonl_watcher:
                        for tool_evt in ms._jsonl_watcher.poll():
                            if tool_evt.get("event") in (
                                    "tool_use", "tool_result"):
                                continue
                            tool_evt["session"] = ms.name
                            self._emit(tool_evt)
                            last_response_change = now

                    # Completion: idle + ✻ marker confirmed + prompt
                    # visible + debounce.  The ❯ prompt is the definitive
                    # "CLI is waiting for input" signal.  Without it, ✻ may
                    # be a per-tool-call marker mid-turn.
                    prompt_visible = any(
                        ln.strip().startswith('❯')
                        for ln in visible.split('\n')[-8:])
                    if (new_state == SessionState.IDLE and turn_confirmed
                            and prompt_visible):
                        if idle_confirmed_since == 0.0:
                            idle_confirmed_since = now
                        if (now - idle_confirmed_since) >= COMPLETION_DEBOUNCE:
                            if not ms._yielded:
                                await asyncio.sleep(0.5)
                                full_capture = await ms.tmux.capture_pane()
                                response = ms.parser.extract_raw_response(
                                    ms._baseline_count, full_capture,
                                    sent_content=getattr(ms, '_sent_content', None))
                                response = _unwrap_tmux_lines(response, getattr(ms, '_pane_width', 0))
                                if response:
                                    self._emit({"session": ms.name,
                                                 "event": "replace",
                                                 "text": response})
                                    ms._yielded = response
                            if ms._yielded:
                                compact_pct_now = _parse_compact_pct(visible)
                                evt = {"session": ms.name,
                                       "event": "complete",
                                       "total_length": len(ms._yielded),
                                       "content": ms._yielded,
                                       "organic": ms._is_organic,
                                       "compact_pct_start": getattr(ms, '_compact_pct_start', None),
                                       "compact_pct_end": compact_pct_now}
                                if ms.channel_id:
                                    evt["channel_id"] = ms.channel_id
                                if ms.team:
                                    evt["team"] = ms.team
                                self._emit(evt)
                                ms.observing = False
                                idle_confirmed_since = 0.0
                                last_observe_complete = now
                                log.info(f"[{ms.name}] Response complete ({len(ms._yielded)} chars, "
                                         f"compact {getattr(ms, '_compact_pct_start', '?')}%→{compact_pct_now}%)")
                                if ms._jsonl_watcher:
                                    meta = ms._jsonl_watcher.get_turn_metadata()
                                    if meta:
                                        self._emit({"session": ms.name,
                                                     "event": "metadata", **meta})
                    else:
                        idle_confirmed_since = 0.0

                    # Quiescence fallback: response stopped growing.
                    # Use short timeout if truly complete (prompt visible),
                    # long timeout otherwise (tool calls can stall response
                    # text for 10-20s while the CLI thinks).
                    if ms._yielded and ms.observing:
                        truly_confirmed = turn_confirmed and prompt_visible
                        q_timeout = (QUIESCENCE_TIMEOUT if truly_confirmed
                                     else QUIESCENCE_UNCONFIRMED_TIMEOUT)
                        if ((now - last_response_change) > q_timeout and
                                new_state in (SessionState.IDLE,
                                              SessionState.RESPONDING)):
                            if getattr(ms, '_streaming_tool_output', False):
                                self._emit({"session": ms.name,
                                             "event": "tool_result_done"})
                                ms._streaming_tool_output = False
                            compact_pct_now = _parse_compact_pct(visible)
                            q_evt = {"session": ms.name, "event": "complete",
                                    "total_length": len(ms._yielded),
                                    "content": ms._yielded,
                                    "reason": "quiescence",
                                    "organic": ms._is_organic,
                                    "compact_pct_start": getattr(ms, '_compact_pct_start', None),
                                    "compact_pct_end": compact_pct_now}
                            if ms.channel_id:
                                q_evt["channel_id"] = ms.channel_id
                            if ms.team:
                                q_evt["team"] = ms.team
                            self._emit(q_evt)
                            ms.observing = False
                            # Longer cooldown after quiescence — the TUI state
                            # may still flicker, causing false re-triggers.
                            last_observe_complete = now + 15.0
                            log.info(f"[{ms.name}] Response complete "
                                     f"(quiescence, confirmed={turn_confirmed}, "
                                     f"compact {getattr(ms, '_compact_pct_start', '?')}%→{compact_pct_now}%)")
                            if ms._jsonl_watcher:
                                meta = ms._jsonl_watcher.get_turn_metadata()
                                if meta:
                                    self._emit({"session": ms.name,
                                                 "event": "metadata", **meta})

                    # No progress timeout — but not while the CLI is
                    # actively thinking (loading context can take 60s+)
                    elif (not ms._yielded and
                            new_state != SessionState.THINKING and
                            (now - last_capture_change) > NO_PROGRESS_TIMEOUT):
                        self._emit({"session": ms.name, "event": "error",
                                     "message": "no progress timeout"})
                        ms.observing = False
                        last_observe_complete = now

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[{ms.name}] Observer error: {e}", exc_info=True)
                await asyncio.sleep(1.0)

    # ── Main loop ───────────────────────────────────────────

    async def run(self):
        log.info(f"lit-bridge starting (mode={'socket' if self._socket_path else 'stdio'})")

        await self._discover_existing()

        self._emit({"event": "monitor_ready",
                     "sessions": len(self.sessions)})

        if self._socket_path:
            await self._run_socket()
        else:
            await self._run_stdio()

        await self._shutdown()

    async def _run_stdio(self):
        """Legacy stdin/stdout mode — dies when parent dies."""
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

    async def _run_socket(self):
        """Unix domain socket mode — survives Connector restarts."""
        sock_path = Path(self._socket_path)
        if sock_path.exists():
            sock_path.unlink()
        sock_path.parent.mkdir(parents=True, exist_ok=True)

        pid_path = sock_path.with_suffix('.pid')
        pid_path.write_text(str(os.getpid()))

        server = await asyncio.start_unix_server(
            self._handle_client, path=str(sock_path)
        )
        os.chmod(str(sock_path), 0o600)
        log.info(f"Listening on {sock_path} (pid={os.getpid()})")

        loop = asyncio.get_event_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        await stop.wait()
        server.close()
        await server.wait_closed()

        for p in (sock_path, pid_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    async def _handle_client(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter):
        """Handle a single Connector client connection."""
        buffered_count = len(self._event_buffer)
        log.info(f"Client connected ({buffered_count} buffered events)")

        old_writer = self._client_writer
        self._client_writer = writer

        # Send monitor_ready first — Connector expects this as the handshake
        session_info = {}
        for name, ms in self.sessions.items():
            session_info[name] = {"working_dir": ms.working_dir}
        self._emit({"event": "monitor_ready",
                     "sessions": len(self.sessions),
                     "session_info": session_info,
                     "buffered": buffered_count})

        # Then flush buffered events from disconnection period
        flushed = 0
        while self._event_buffer:
            line = self._event_buffer.popleft()
            try:
                writer.write(line.encode('utf-8') if isinstance(line, str) else line)
                flushed += 1
            except (ConnectionError, RuntimeError):
                self._client_writer = None
                return
        if flushed:
            log.info(f"Flushed {flushed} buffered events to client")

        try:
            while self._running:
                line = await reader.readline()
                if not line:
                    break

                text = line.decode('utf-8', errors='replace').strip()
                if not text:
                    continue

                try:
                    cmd = json.loads(text)
                except json.JSONDecodeError as e:
                    self._emit({"event": "error",
                                 "message": f"invalid JSON: {e}"})
                    continue

                await self.handle_command(cmd)

        except (asyncio.CancelledError, ConnectionError):
            pass
        finally:
            if self._client_writer is writer:
                self._client_writer = None
            writer.close()
            log.info(f"Client disconnected (buffering events)")

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
            parser = select_parser("claude-code")

            visible = await tmux.capture_pane(visible_only=True)
            if not visible.strip():
                continue

            win_name = await tmux.get_window_name()
            channel_id = None
            team = None
            if win_name and win_name not in CLI_DEFAULTS:
                if ':' in win_name:
                    team, channel_id = win_name.split(':', 1)
                else:
                    channel_id = win_name
            working_dir = await tmux.get_pane_cwd() or None
            key = self._session_key(session_name, channel_id)
            ms = ManagedSession(key, tmux, parser,
                                working_dir=working_dir,
                                channel_id=channel_id, team=team)
            ms.state = parser.detect_state(visible)
            ms._observe_task = asyncio.create_task(self._observe_loop(ms))
            self.sessions[key] = ms

            log.info(f"Discovered existing session: {key} "
                     f"(state={ms.state.value} channel={channel_id} team={team} "
                     f"working_dir={working_dir})")

    async def _shutdown(self):
        log.info(f"Shutting down ({len(self.sessions)} sessions still running)")
        self._running = False
        for ms in self.sessions.values():
            if ms._observe_task:
                ms._observe_task.cancel()
        self._emit({"event": "monitor_shutdown"})


def _run_test_parser():
    """Run snapshot-based parser tests."""
    tests_dir = Path(__file__).parent / "tests"
    if not (tests_dir / "test_parsers.py").exists():
        print("No test suite found at tests/test_parsers.py")
        raise SystemExit(1)
    import subprocess
    result = subprocess.run(
        [sys.executable, str(tests_dir / "test_parsers.py")],
        cwd=str(Path(__file__).parent),
    )
    raise SystemExit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="lit-bridge: AI CLI session multiplexer")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("test-parser", help="Run snapshot-based parser validation tests")

    parser.add_argument("--socket", metavar="PATH",
                        help="Listen on a Unix domain socket (daemon mode)")
    args = parser.parse_args()

    if args.command == "test-parser":
        _run_test_parser()
        return

    if args.socket:
        log_path = Path(args.socket).with_suffix('.log')
        fh = logging.FileHandler(str(log_path))
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(fh)

    monitor = Monitor(socket_path=args.socket)
    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
