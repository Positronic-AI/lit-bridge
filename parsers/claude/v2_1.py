"""Claude Code TUI parser — validated against CLI v2.1.x.

All knowledge of Claude Code's terminal rendering lives here.
When Anthropic updates the TUI, copy this file to a new version
module (e.g. v2_2.py) and update the patterns there.
"""

import re
from typing import List, Optional, Tuple

from ..base import TUIParser, TUIMessage, TUIContentBlock, TUIState, SessionState

SUPPORTED_VERSIONS = ("2.1",)
VALIDATED_DATE = "2026-05-28"
VALIDATED_BUILD = "2.1.152"


class ClaudeV21Parser(TUIParser):

    RE_USER = re.compile(r'^\s*❯\s')
    RE_RESPONSE = re.compile(r'^\s*●\s')
    RE_COMPLETION = re.compile(r'^\s*✻\s+(?!.*…)(.+)')
    RE_SPINNER_ACTIVE = re.compile(r'^\s*✻\s+.*…')
    RE_THINKING_SPINNER = re.compile(r'^\s*✽\s')
    RE_SEPARATOR = re.compile(r'^─{10,}$')
    RE_STATUS = re.compile(r'^\s*⏵')
    RE_VERSION = re.compile(r'Claude Code (v[\d.]+)')
    RE_DIALOG_SELECTION = re.compile(r'❯\s+\d+\.\s')
    RE_SPINNER_LINE = re.compile(r'^\s*[✽✢]\s')
    RE_TOOL_INDICATOR = re.compile(r'^\s*⎿\s')
    RE_TOOL_HEADER = re.compile(r'^\s*●\s+(Reading|Writing|Editing|Running|Searching|Listing)\s')
    RE_TOOL_CALL_START = re.compile(r'^([A-Z]\w*)\(')
    RE_TOKEN_STATS = re.compile(r'\(.*?[↓↑]\s*\d+\s*tokens?.*?\)')

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
        s = line.strip()
        if not s:
            return False
        if self.RE_SPINNER_LINE.match(s):
            return True
        if self.RE_TOOL_INDICATOR.match(s):
            return True
        if self.RE_TOOL_HEADER.match(s):
            return True
        if self.RE_TOKEN_STATS.search(s):
            return True
        if 'ctrl+o to expand' in s or 'ctrl+e to expand' in s:
            return True
        return False

    def detect_state(self, capture: str) -> SessionState:
        if not capture or not capture.strip():
            return SessionState.DEAD

        # Only check bottom of screen for dialogs — checking the full
        # capture causes false positives when conversation text contains
        # dialog strings like "Enter to confirm".
        bottom = '\n'.join(capture.strip().split('\n')[-10:])
        for s in self.DIALOG_STRINGS:
            if s in bottom:
                return SessionState.DIALOG
        if self.RE_DIALOG_SELECTION.search(bottom):
            return SessionState.DIALOG

        lines = [line.strip() for line in capture.strip().split('\n') if line.strip()]
        if not lines:
            return SessionState.DEAD

        has_interrupt = False
        has_prompt = False
        for line in reversed(lines):
            if self.RE_STATUS.match(line):
                if 'esc to interrupt' in line or 'interrupt' in line:
                    has_interrupt = True
                break
            if self.RE_SEPARATOR.match(line):
                continue
            if line.startswith('❯'):
                has_prompt = True
                continue
            break

        if has_interrupt:
            return SessionState.THINKING

        # ❯ prompt visible with no "esc to interrupt" = definitively idle.
        # This catches interrupted responses where the last ● has no ✻.
        if has_prompt:
            return SessionState.IDLE

        for line in reversed(lines):
            if self.RE_STATUS.match(line):
                continue
            if self.RE_SEPARATOR.match(line):
                continue
            if line.startswith('❯'):
                continue
            if self.RE_COMPLETION.match(line):
                return SessionState.IDLE
            if self.RE_SPINNER_ACTIVE.match(line):
                return SessionState.THINKING
            if self.RE_THINKING_SPINNER.match(line):
                return SessionState.THINKING
            break

        last_bullet = capture.rfind('●')
        if last_bullet >= 0:
            after = capture[last_bullet:]
            if '✻' not in after and '✽' not in after:
                return SessionState.RESPONDING

        return SessionState.IDLE

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

    def extract_content_blocks(self, capture: str) -> List[TUIContentBlock]:
        lines = capture.split('\n')
        blocks: List[TUIContentBlock] = []
        i = 0

        while i < len(lines):
            stripped = lines[i].strip()

            if (not stripped or
                    self.RE_USER.match(stripped) or
                    self.RE_SEPARATOR.match(stripped) or
                    self.RE_STATUS.match(stripped) or
                    self.RE_COMPLETION.match(stripped) or
                    self.RE_SPINNER_LINE.match(stripped) or
                    self.RE_TOKEN_STATS.search(stripped)):
                i += 1
                continue

            if self.RE_RESPONSE.match(stripped):
                first_line = re.sub(r'^\s*●\s*', '', stripped)
                tool_match = self.RE_TOOL_CALL_START.match(first_line)

                if tool_match:
                    tool_name = tool_match.group(1)
                    tool_input = first_line[len(tool_name):]
                    if tool_input.startswith('('):
                        tool_input = tool_input[1:]
                    if tool_input.endswith(')'):
                        tool_input = tool_input[:-1]

                    blocks.append(TUIContentBlock(
                        type="tool_call",
                        content=tool_input.strip(),
                        tool_name=tool_name,
                    ))

                    i += 1
                    output_lines = []
                    while i < len(lines):
                        s = lines[i].strip()
                        if (self.RE_RESPONSE.match(s) or
                                self.RE_USER.match(s) or
                                self.RE_SEPARATOR.match(s) or
                                self.RE_STATUS.match(s) or
                                self.RE_COMPLETION.match(s)):
                            break
                        if self.RE_TOKEN_STATS.search(s):
                            i += 1
                            continue
                        if self.RE_SPINNER_LINE.match(s):
                            i += 1
                            continue
                        output_lines.append(lines[i].rstrip())
                        i += 1
                    if output_lines:
                        output = '\n'.join(output_lines).rstrip()
                        if output:
                            blocks.append(TUIContentBlock(
                                type="tool_output", content=output))
                    continue

                content_lines = []
                if first_line and not self._is_tui_chrome('● ' + first_line):
                    content_lines.append(first_line)
                i += 1
                while i < len(lines):
                    s = lines[i].strip()
                    if (self.RE_RESPONSE.match(s) or
                            self.RE_USER.match(s) or
                            self.RE_SEPARATOR.match(s) or
                            self.RE_STATUS.match(s) or
                            self.RE_COMPLETION.match(s)):
                        break
                    if self._is_tui_chrome(s):
                        i += 1
                        continue
                    content_lines.append(lines[i].rstrip())
                    i += 1
                text = '\n'.join(content_lines).rstrip()
                if text:
                    blocks.append(TUIContentBlock(type="text", content=text))
                continue

            i += 1

        return blocks

    def count_assistant_messages(self, capture: str) -> int:
        return sum(1 for line in capture.split('\n')
                   if self.RE_RESPONSE.match(line.strip()))

    def extract_new_response(self, baseline_count: int, capture: str) -> str:
        messages = self.extract_messages(capture)
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        if len(assistant_msgs) <= baseline_count:
            return ""
        new_msgs = assistant_msgs[baseline_count:]
        return "\n\n".join(m.content for m in new_msgs)

    def extract_raw_response(self, baseline_count: int, capture: str,
                             sent_content: str = None) -> str:
        """Faithful capture: everything from the first new ● to ✻ or end.

        Strips trailing TUI chrome (separator, prompt, status bar) so
        the captured text stabilises when the response content stops
        changing — even while the CLI animates spinners in the chrome.

        When the scrollback buffer has truncated old messages (making
        the absolute baseline_count unreachable), falls back to
        locating the sent message text as a landmark.
        """
        lines = capture.split('\n')
        bullet_count = 0
        start_idx = None
        end_idx = len(lines)

        for i, line in enumerate(lines):
            if self.RE_RESPONSE.match(line.strip()):
                bullet_count += 1
                if bullet_count == baseline_count + 1 and start_idx is None:
                    start_idx = i

            if start_idx is not None and self.RE_COMPLETION.match(line.strip()):
                end_idx = i + 1  # include the ✻ line
                break

        # Scrollback truncation: old messages fell off the buffer.
        # Find the sent message text as a landmark instead.
        if start_idx is None and bullet_count < baseline_count and sent_content:
            needle = sent_content.strip().split('\n')[0][:80]
            sent_line_idx = None
            for i, line in enumerate(lines):
                if needle and needle in line:
                    sent_line_idx = i
                    break
            if sent_line_idx is not None:
                for i in range(sent_line_idx + 1, len(lines)):
                    if self.RE_RESPONSE.match(lines[i].strip()):
                        start_idx = i
                        break
                if start_idx is not None:
                    end_idx = len(lines)
                    for i in range(start_idx + 1, len(lines)):
                        if self.RE_COMPLETION.match(lines[i].strip()):
                            end_idx = i + 1
                            break

        if start_idx is None:
            return ""

        # Strip trailing TUI chrome that changes every poll
        while end_idx > start_idx:
            s = lines[end_idx - 1].strip()
            if (not s or
                    self.RE_SEPARATOR.match(s) or
                    self.RE_USER.match(s) or
                    self.RE_SPINNER_ACTIVE.match(s) or
                    self.RE_STATUS.match(s) or
                    self.RE_TOKEN_STATS.search(s) or
                    'Claude Code' in s):
                end_idx -= 1
            else:
                break

        return '\n'.join(lines[start_idx:end_idx]).rstrip()

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
