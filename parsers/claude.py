"""Claude Code TUI parser.

All knowledge of Claude Code's terminal rendering lives here.
When Anthropic updates the TUI, update this file — nothing else changes.
"""

import re
from typing import List, Optional, Tuple

from .base import TUIParser, TUIMessage, TUIState, SessionState


class ClaudeTUIParser(TUIParser):

    RE_USER = re.compile(r'^\s*❯\s')
    RE_RESPONSE = re.compile(r'^\s*●\s')
    RE_COMPLETION = re.compile(r'^\s*✻\s+(.+)')
    RE_THINKING_SPINNER = re.compile(r'^\s*✽\s')
    RE_SEPARATOR = re.compile(r'^─{10,}$')
    RE_STATUS = re.compile(r'^\s*⏵')
    RE_VERSION = re.compile(r'Claude Code (v[\d.]+)')
    RE_DIALOG_SELECTION = re.compile(r'❯\s+\d+\.\s')
    RE_SPINNER_LINE = re.compile(r'^\s*[✽✢]\s')
    RE_TOOL_INDICATOR = re.compile(r'^\s*⎿\s')
    RE_TOOL_HEADER = re.compile(r'^\s*●\s+(Reading|Writing|Editing|Running|Searching|Listing)\s')
    RE_TOKEN_STATS = re.compile(r'\(.*?[↓↑]\s*\d+\s*tokens?\)')

    DIALOG_STRINGS = [
        "Enter to confirm",
        "Esc to cancel",
    ]

    STARTUP_DIALOGS = [
        ("Select login method", ["Enter"], "login-method"),
        ("Yes, I accept", ["Down", "Enter"], "bypass-permissions"),
        ("Choose the text style", ["Enter"], "theme-selection"),
        ("enable auto mode", ["Enter"], "auto-mode"),
        ("Do you trust", ["Enter"], "workspace-trust"),
        ("I trust this folder", ["Enter"], "workspace-trust"),
        ("safety check", ["Enter"], "workspace-trust"),
    ]

    def _is_tui_chrome(self, line: str) -> bool:
        """Return True if this line is TUI chrome, not real content."""
        s = line.strip()
        if not s:
            return False
        if self.RE_SPINNER_LINE.match(s):
            return True
        if self.RE_TOOL_INDICATOR.match(s):
            return True
        if self.RE_TOOL_HEADER.match(s):
            return True
        if self.RE_TOKEN_STATS.match(s):
            return True
        if 'ctrl+o to expand' in s or 'ctrl+e to expand' in s:
            return True
        return False

    def detect_state(self, capture: str) -> SessionState:
        if not capture or not capture.strip():
            return SessionState.DEAD

        for s in self.DIALOG_STRINGS:
            if s in capture:
                return SessionState.DIALOG
        if self.RE_DIALOG_SELECTION.search(capture):
            return SessionState.DIALOG

        lines = [line.strip() for line in capture.strip().split('\n') if line.strip()]
        if not lines:
            return SessionState.DEAD

        for line in reversed(lines):
            if self.RE_STATUS.match(line):
                return SessionState.IDLE
            if self.RE_SEPARATOR.match(line):
                continue
            if line == '❯':
                return SessionState.IDLE
            if self.RE_COMPLETION.match(line):
                return SessionState.IDLE
            if self.RE_THINKING_SPINNER.match(line):
                return SessionState.THINKING
            break

        last_bullet = capture.rfind('●')
        if last_bullet >= 0:
            after = capture[last_bullet:]
            if '✻' not in after and '✽' not in after:
                return SessionState.RESPONDING

        return SessionState.THINKING

    def extract_messages(self, capture: str) -> List[TUIMessage]:
        lines = capture.split('\n')
        messages: List[TUIMessage] = []
        i = 0

        while i < len(lines):
            stripped = lines[i].strip()

            if self.RE_USER.match(stripped) and len(stripped) > 2:
                text = re.sub(r'^\s*❯\s*', '', stripped)
                i += 1
                while i < len(lines):
                    s = lines[i].strip()
                    if (self.RE_RESPONSE.match(s) or
                            self.RE_SEPARATOR.match(s) or
                            self.RE_STATUS.match(s) or
                            (self.RE_USER.match(s) and len(s) > 2)):
                        break
                    if s:
                        text += '\n' + s
                    i += 1
                messages.append(TUIMessage(role="user", content=text.strip()))
                continue

            if self.RE_RESPONSE.match(stripped):
                first_line = re.sub(r'^\s*●\s*', '', stripped)
                duration = None
                content_lines = []
                if first_line and not self._is_tui_chrome('● ' + first_line):
                    content_lines.append(first_line)
                i += 1
                while i < len(lines):
                    s = lines[i].strip()
                    m = self.RE_COMPLETION.match(s)
                    if m:
                        duration = m.group(1)
                        i += 1
                        break
                    if (self.RE_RESPONSE.match(s) or
                            self.RE_SEPARATOR.match(s) or
                            self.RE_STATUS.match(s) or
                            self.RE_USER.match(s)):
                        break
                    if not self._is_tui_chrome(s):
                        content_lines.append(lines[i].rstrip())
                    i += 1
                text = '\n'.join(content_lines).rstrip()
                messages.append(TUIMessage(
                    role="assistant",
                    content=text,
                    duration=duration,
                ))
                continue

            i += 1

        return messages

    def count_assistant_messages(self, capture: str) -> int:
        return sum(1 for line in capture.split('\n')
                   if self.RE_RESPONSE.match(line.strip()))

    def extract_new_response(self, baseline_count: int, capture: str) -> str:
        messages = self.extract_messages(capture)
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        if len(assistant_msgs) <= baseline_count:
            return ""
        # Concatenate ALL new assistant blocks (tool calls + text)
        new_msgs = assistant_msgs[baseline_count:]
        return "\n\n".join(m.content for m in new_msgs)

    def is_startup_dialog(self, capture: str) -> Optional[Tuple[str, List[str], str]]:
        for detect, keys, name in self.STARTUP_DIALOGS:
            if detect in capture:
                return (detect, keys, name)
        return None

    def parse(self, capture: str) -> TUIState:
        state = self.detect_state(capture)
        messages = self.extract_messages(capture)
        version = None
        m = self.RE_VERSION.search(capture[:300])
        if m:
            version = m.group(1)
        errors = []
        if '✗' in capture:
            for line in capture.split('\n'):
                if '✗' in line:
                    errors.append(line.strip())
        return TUIState(
            state=state,
            messages=messages,
            version=version,
            errors=errors,
        )
