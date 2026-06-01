"""Tmux session lifecycle management.

Handles spawn, capture, send, kill for a single tmux session.
No LIT dependencies — this is a generic tmux wrapper.
"""

import asyncio
import re
import shlex
import time
from typing import Optional


TMUX_WIDTH = 200
TMUX_HEIGHT = 50


def sanitize_name(s: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]', '-', s or 'x')


class TmuxSession:

    def __init__(self, session_name: str, window_name: str = None):
        self.session_name = session_name
        self.window_name = window_name
        self.created_at = time.monotonic()
        self.last_used = time.monotonic()
        self.message_count = 0
        self._alive = False

    @property
    def _target(self) -> str:
        if self.window_name:
            return f"{self.session_name}:{self.window_name}"
        return self.session_name

    def touch(self):
        self.last_used = time.monotonic()

    async def _exec(self, cmd: str, input_data: bytes = None,
                    timeout: float = 10.0) -> tuple:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdin=asyncio.subprocess.PIPE if input_data else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_data), timeout=timeout
        )
        return (proc.returncode,
                stdout.decode('utf-8', errors='replace'),
                stderr.decode('utf-8', errors='replace'))

    async def session_exists(self) -> bool:
        cmd = f"tmux has-session -t {shlex.quote(self.session_name)}"
        rc, _, _ = await self._exec(cmd)
        return rc == 0

    async def spawn(self, cmd: list, env_vars: dict = None,
                    working_dir: str = None):
        tmux_args = [
            "tmux", "new-session", "-d",
            "-s", self.session_name,
            "-x", str(TMUX_WIDTH),
            "-y", str(TMUX_HEIGHT),
        ]
        if self.window_name:
            tmux_args.extend(["-n", self.window_name])
        if working_dir:
            tmux_args.extend(["-c", working_dir])
        for k, v in (env_vars or {}).items():
            tmux_args.extend(["-e", f"{k}={v}"])
        tmux_args.extend(cmd)

        tmux_cmd = " ".join(shlex.quote(a) for a in tmux_args)
        rc, _, stderr = await self._exec(tmux_cmd)
        if rc != 0:
            raise RuntimeError(f"tmux new-session failed: {stderr.strip()}")
        self._alive = True

    async def create_window(self, cmd: list, env_vars: dict = None,
                            working_dir: str = None):
        """Create a new window in an existing tmux session."""
        tmux_args = [
            "tmux", "new-window",
            "-t", self.session_name,
            "-n", self.window_name or "default",
        ]
        if working_dir:
            tmux_args.extend(["-c", working_dir])
        for k, v in (env_vars or {}).items():
            tmux_args.extend(["-e", f"{k}={v}"])
        tmux_args.extend(cmd)

        tmux_cmd = " ".join(shlex.quote(a) for a in tmux_args)
        rc, _, stderr = await self._exec(tmux_cmd)
        if rc != 0:
            raise RuntimeError(f"tmux new-window failed: {stderr.strip()}")
        self._alive = True

    async def capture_pane(self, visible_only: bool = False) -> str:
        """Capture the tmux pane content.

        visible_only=False (default): full scrollback (-S -), for message extraction.
        visible_only=True: visible screen only (-S 0), for state detection.
        Scrollback includes dismissed dialogs whose text confuses state detection.
        """
        start_flag = "-S 0" if visible_only else "-S -"
        cmd = f"tmux capture-pane -t {shlex.quote(self._target)} -p {start_flag}"
        rc, stdout, stderr = await self._exec(cmd)
        if rc != 0:
            if any(s in stderr for s in
                   ("no server", "session not found", "can't find",
                    "window not found")):
                self._alive = False
            return ""
        return stdout

    async def send_message(self, text: str):
        self.touch()
        if not self._alive:
            raise RuntimeError("tmux session is not alive")

        rc, _, stderr = await self._exec(
            "tmux load-buffer -", input_data=text.encode('utf-8')
        )
        if rc != 0:
            raise RuntimeError(f"tmux load-buffer failed: {stderr}")

        target = shlex.quote(self._target)
        rc, _, stderr = await self._exec(f"tmux paste-buffer -t {target}")
        if rc != 0:
            raise RuntimeError(f"tmux paste-buffer failed: {stderr}")

        rc, _, stderr = await self._exec(f"tmux send-keys -t {target} Enter")
        if rc != 0:
            raise RuntimeError(f"tmux send-keys failed: {stderr}")

        self.message_count += 1

    async def send_keys(self, keys: str):
        target = shlex.quote(self._target)
        await self._exec(f"tmux send-keys -t {target} {keys}")

    async def rename_window(self, name: str):
        target = shlex.quote(self._target)
        await self._exec(f"tmux rename-window -t {target} {shlex.quote(name)}")

    async def get_window_name(self) -> str:
        target = shlex.quote(self._target)
        rc, stdout, _ = await self._exec(
            f"tmux display-message -t {target} -p '#W'")
        return stdout.strip() if rc == 0 else ""

    async def get_pane_cwd(self) -> str:
        target = shlex.quote(self._target)
        rc, stdout, _ = await self._exec(
            f"tmux display-message -t {target} -p '#{{pane_current_path}}'")
        return stdout.strip() if rc == 0 else ""

    async def is_alive(self) -> bool:
        if self.window_name:
            cmd = f"tmux list-windows -t {shlex.quote(self.session_name)} -F '#{{window_name}}'"
            rc, stdout, _ = await self._exec(cmd)
            self._alive = rc == 0 and self.window_name in stdout.split('\n')
        else:
            cmd = f"tmux has-session -t {shlex.quote(self.session_name)}"
            rc, _, _ = await self._exec(cmd)
            self._alive = (rc == 0)
        return self._alive

    async def kill(self):
        try:
            await self._exec(
                f"tmux kill-session -t {shlex.quote(self.session_name)}"
            )
        except Exception:
            pass
        self._alive = False
