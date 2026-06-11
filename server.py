#!/usr/bin/env python3
"""lit-bridge: AI CLI session multiplexer.

A lightweight daemon that manages interactive AI CLI sessions in tmux.
Speaks JSON-lines protocol. Knows nothing about LIT.

Modes:
  stdio:   reads stdin, writes stdout (legacy, dies with parent)
  socket:  listens on a Unix domain socket (daemon, survives API restarts)

Usage:
  python3 server.py                          # stdio mode
  python3 server.py --socket /tmp/lit-bridge-ben.sock   # socket mode

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
from jsonl_watcher import JsonlWatcher, cc_project_dir
from observer import observe_loop, _parse_compact_pct, _unwrap_tmux_lines

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("lit-bridge")

STARTUP_WAIT = 3.0
EVENT_BUFFER_MAX = 500


CLI_DEFAULTS = {
    "claude": "claude",
}


def find_cli(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise FileNotFoundError(f"CLI not found: {name}")
    return path


class ManagedSession:
    """A tmux session + its parser + observer state."""

    def __init__(self, name: str, tmux: TmuxSession, parser: TUIParser,
                 working_dir: str = None, channel_id: str = None,
                 team: str = None, config_dir: str = None):
        self.name = name
        self.tmux = tmux
        self.parser = parser
        self.working_dir = working_dir
        self.channel_id = channel_id
        self.team = team
        self.state = SessionState.STARTING
        self.model: Optional[str] = None
        self.observing = False
        self._is_organic = False
        self._observe_task: Optional[asyncio.Task] = None
        self._jsonl_watcher: Optional[JsonlWatcher] = None
        self._claude_session_id: Optional[str] = None
        self._last_active: float = time.monotonic()
        self._paused: bool = False
        self._yielded: str = ""
        if working_dir:
            project_dir = cc_project_dir(working_dir, config_dir)
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

    @staticmethod
    def _model_from_command(cmdline: str) -> Optional[str]:
        """Extract --model value from a CLI command line / arg list."""
        parts = cmdline.split() if isinstance(cmdline, str) else list(cmdline)
        for i, p in enumerate(parts):
            if p == "--model" and i + 1 < len(parts):
                return parts[i + 1]
        return None

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
                if ms.model is None:
                    ms.model = self._model_from_command(
                        await ms.tmux.get_pane_command())
                self._emit({"session": session_key, "event": "ready",
                            "reused": True, "model": ms.model})
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
        resume_session_id = reaped.get("session_id") if reaped else None
        if resume_session_id:
            cli_args.extend(["--resume", resume_session_id])
            log.info(f"[{session_key}] Resuming from reaped session {resume_session_id}")

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
                                channel_id=channel_id, team=team,
                                config_dir=env_vars.get("CLAUDE_CONFIG_DIR"))
            self.sessions[session_key] = ms
            capture = await orphan_tmux.capture_pane(visible_only=True)
            ms.state = parser.detect_state(capture)
            ms.model = self._model_from_command(
                await orphan_tmux.get_pane_command())
            ms._observe_task = asyncio.create_task(self._observe_loop(ms))
            self._emit({"session": session_key, "event": "ready",
                        "reused": True, "model": ms.model})
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
                            channel_id=channel_id, team=team,
                            config_dir=env_vars.get("CLAUDE_CONFIG_DIR"))
        self.sessions[session_key] = ms

        if win_label:
            await tmux.rename_window(win_label)
            tmux.window_name = win_label

        await asyncio.sleep(STARTUP_WAIT)

        # Auto-dismiss startup dialogs
        for attempt in range(5):
            if not await tmux.is_alive():
                # If we were resuming, retry without --resume (session may be stale)
                if resume_session_id:
                    log.info(f"[{session_key}] Resume failed — retrying without --resume")
                    await tmux.kill()
                    del self.sessions[session_key]
                    fresh_args = [a for a in cli_args if a != "--resume" and a != resume_session_id]
                    retry_cmd = dict(cmd)
                    retry_cmd["args"] = fresh_args
                    await self._cmd_create(retry_cmd)
                    return
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
            if resume_session_id:
                log.info(f"[{session_key}] Resume failed (post-startup) — retrying without --resume")
                await tmux.kill()
                del self.sessions[session_key]
                fresh_args = [a for a in cli_args if a != "--resume" and a != resume_session_id]
                retry_cmd = dict(cmd)
                retry_cmd["args"] = fresh_args
                await self._cmd_create(retry_cmd)
                return
            self._emit_error(session_key, "session died after startup")
            await tmux.kill()
            del self.sessions[session_key]
            return

        capture = await tmux.capture_pane(visible_only=True)
        ms.state = parser.detect_state(capture)
        ms.model = self._model_from_command(cli_args)

        self._emit({"session": session_key, "event": "ready",
                    "state": ms.state.value, "model": ms.model,
                    "resumed": resume_session_id is not None})

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

        # Emit boundary for previous turn (message-boundary completion)
        if getattr(ms, '_yielded', '') and ms.channel_id:
            evt = {"session": sk, "event": "boundary",
                   "content": ms._yielded}
            if ms.channel_id:
                evt["channel_id"] = ms.channel_id
            if ms.team:
                evt["team"] = ms.team
            self._emit(evt)
            log.info(f"[{sk}] Boundary: {len(ms._yielded)} chars")
            ms.observing = False
            ms._yielded = ""
            ms._paused = False

        # Check session state but don't block — send regardless, like typing in tmux.
        # Dialog state is the only hard block (needs keystroke, not text input).
        visible = await ms.tmux.capture_pane(visible_only=True)
        current_state = ms.parser.detect_state(visible)

        if current_state == SessionState.DIALOG:
            self._emit({"session": sk, "event": "error",
                         "message": "session is in dialog state, use keystroke command"})
            return

        # Ctrl+O expanded transcript mode — CLI won't accept input.
        # Check only the last 3 lines (footer area) to avoid matching
        # against the string appearing in code/response content.
        footer_lines = '\n'.join(visible.strip().split('\n')[-3:]).lower()
        if 'showing detailed transcript' in footer_lines:
            log.info(f"[{sk}] Blocked send — CLI is in Ctrl+O expanded mode")
            self._emit({"session": sk, "event": "error",
                         "message": "CLI is in expanded transcript mode (Ctrl+O). Toggle back to send messages."})
            return

        if current_state not in (SessionState.IDLE, SessionState.THINKING):
            log.info(f"[{sk}] Sending into {current_state.value} state (CLI will queue input)")

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
        ms._jsonl_content_set = False
        ms._turn_confirmed = False
        ms._compact_pct_start = _parse_compact_pct(visible)
        ms._baseline_completion_count = sum(
            1 for ln in full_capture.split('\n')
            if re.match(r'^\s*✻\s', ln))
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
        channel_id = cmd.get("channel_id")
        session_key = self._session_key(name, channel_id)
        content = cmd.get("content", "")

        ms = self.sessions.get(session_key)
        if not ms and channel_id:
            ms = self.sessions.get(name)
        if not ms:
            self._emit_error(session_key, "session not found")
            return

        try:
            await ms.tmux.send_message(content)
            self._emit({"session": ms.name, "event": "input_sent"})
        except RuntimeError as e:
            self._emit_error(ms.name, f"input failed: {e}")

    async def _cmd_keystroke(self, cmd: dict):
        name = cmd.get("session")
        channel_id = cmd.get("channel_id")
        session_key = self._session_key(name, channel_id)
        keys = cmd.get("keys", [])

        ms = self.sessions.get(session_key)
        if not ms and channel_id:
            ms = self.sessions.get(name)
        if not ms:
            self._emit_error(session_key, "session not found")
            return

        for key in keys:
            await ms.tmux.send_keys(key)
            await asyncio.sleep(0.1)

        self._emit({"session": ms.name, "event": "keystroke_sent", "keys": keys})

    async def _cmd_kill(self, cmd: dict):
        name = cmd.get("session")
        channel_id = cmd.get("channel_id")
        session_key = self._session_key(name, channel_id)

        ms = self.sessions.get(session_key)
        if not ms and channel_id:
            ms = self.sessions.get(name)
        if not ms:
            self._emit_error(session_key, "session not found")
            return

        # Stash the CLI session id so the next create resumes the
        # conversation (used for model switches: kill + recreate)
        if cmd.get("store_resume"):
            session_id = None
            if ms._jsonl_watcher:
                session_id = ms._jsonl_watcher.get_session_id()
            self._reaped_sessions[ms.name] = {
                "session_id": session_id,
                "working_dir": ms.working_dir,
            }
            log.info(f"[{ms.name}] Killed with resume stash "
                     f"(resume_id={session_id})")

        if ms._observe_task:
            ms._observe_task.cancel()
        await ms.tmux.kill()
        self.sessions.pop(ms.name, None)
        self._emit({"session": ms.name, "event": "killed"})

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
        """Delegate to observer.observe_loop with Monitor callbacks."""
        def _on_reap(session):
            session_id = None
            if session._jsonl_watcher:
                session_id = session._jsonl_watcher.get_session_id()
            self._reaped_sessions[session.name] = {
                "session_id": session_id,
                "working_dir": session.working_dir,
            }
            log.info(
                f"[{session.name}] Reaping idle session "
                f"(resume_id={session_id})"
            )
            asyncio.create_task(session.tmux.kill())
            self._emit({"session": session.name, "event": "reaped",
                         "resume_session_id": session_id})
            self.sessions.pop(session.name, None)

        await observe_loop(
            ms=ms,
            emit=self._emit,
            is_running=lambda: self._running,
            has_client=lambda: self._client_writer is not None,
            on_reap=_on_reap,
        )

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
        """Find running lit-* tmux sessions and all their windows on startup."""
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

            # List all windows in this session
            try:
                proc = await asyncio.create_subprocess_shell(
                    f"tmux list-windows -t {shlex.quote(session_name)}"
                    f" -F '#{{window_name}}'",
                    stdout=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                window_names = [
                    w.strip() for w in
                    stdout.decode('utf-8', errors='replace').strip().split('\n')
                    if w.strip()
                ]
            except Exception:
                window_names = []

            if not window_names:
                continue

            for win_name in window_names:
                tmux = TmuxSession(session_name, window_name=win_name)
                tmux._alive = True
                parser = select_parser("claude-code")

                visible = await tmux.capture_pane(visible_only=True)
                if not visible.strip():
                    continue

                channel_id = None
                team = None
                if win_name not in CLI_DEFAULTS:
                    if ':' in win_name:
                        team, channel_id = win_name.split(':', 1)
                    else:
                        channel_id = win_name
                working_dir = await tmux.get_pane_cwd() or None
                # Derive CLAUDE_CONFIG_DIR from session name convention:
                # lit-{username}-{agent_id} → ~/.claude-{agent_id}
                config_dir = None
                parts = session_name.split('-', 2)
                if len(parts) >= 3:
                    agent_id = parts[2]
                    user_home = str(Path(f"/home/{parts[1]}"))
                    candidate = f"{user_home}/.claude-{agent_id}"
                    if Path(candidate).is_dir():
                        config_dir = candidate
                key = self._session_key(session_name, channel_id)
                ms = ManagedSession(key, tmux, parser,
                                    working_dir=working_dir,
                                    channel_id=channel_id, team=team,
                                    config_dir=config_dir)
                ms.state = parser.detect_state(visible)
                ms.model = self._model_from_command(
                    await tmux.get_pane_command())
                ms._observe_task = asyncio.create_task(self._observe_loop(ms))
                self.sessions[key] = ms

                log.info(f"Discovered existing session: {key} "
                         f"(state={ms.state.value} channel={channel_id} team={team} "
                         f"window={win_name} working_dir={working_dir})")

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
