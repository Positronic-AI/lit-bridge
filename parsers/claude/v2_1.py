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
    RE_SPINNER_ACTIVE = re.compile(r'^\s*[·✢-✿]\s+.*…')
    RE_THINKING_SPINNER = re.compile(r'^\s*[·✢-✿]\s')
    RE_SEPARATOR = re.compile(r'^─{10,}$')
    RE_STATUS = re.compile(r'^\s*[⏵▸]')
    RE_VERSION = re.compile(r'Claude Code (v[\d.]+)')
    RE_DIALOG_SELECTION = re.compile(r'❯\s+\d+\.\s')
    RE_SPINNER_LINE = re.compile(r'^\s*[·✢-✿]\s')
    RE_TOOL_INDICATOR = re.compile(r'^\s*⎿\s')
    RE_TOOL_HEADER = re.compile(r'^\s*●\s+(Reading|Writing|Editing|Running|Searching|Listing)\s')
    RE_TOOL_CALL_START = re.compile(r'^([A-Z]\w*)\(')
    RE_TOKEN_STATS = re.compile(r'\(.*?[↓↑]\s*[\d.]+k?\s*tokens?.*?\)')
    RE_CONVERSATION_PICKER = re.compile(
        r'^\s*[●○◯]\s+\S.*(?:↑/↓|to select|Enter to view|\d+[ms]\d*s?)'
        r'|^\s*[●○◯]\s*(?:Explore|Plan|main)\b'
    )
    RE_COMPACT_PROGRESS = re.compile(r'^\d+%\s+until\s+auto-compact')

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
            if self.RE_COMPACT_PROGRESS.match(line):
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
            if self.RE_COMPACT_PROGRESS.match(line):
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

        # Strip conversation picker before checking for active response
        content_lines = capture.split('\n')
        while content_lines:
            s = content_lines[-1].strip()
            if (not s or self.RE_CONVERSATION_PICKER.match(s) or
                    self.RE_SEPARATOR.match(s) or s.startswith('❯') or
                    self.RE_STATUS.match(s) or
                    self.RE_COMPACT_PROGRESS.match(s)):
                content_lines.pop()
            else:
                break
        content_area = '\n'.join(content_lines)
        last_bullet = content_area.rfind('●')
        if last_bullet >= 0:
            after = content_area[last_bullet:]
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
                    self.RE_COMPLETION.match(stripped)):
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
                             sent_content: str = None,
                             baseline_completions: int = 0) -> str:
        """Faithful capture: everything from the first new ● to ✻ or end.

        Strips trailing TUI chrome (separator, prompt, status bar) so
        the captured text stabilises when the response content stops
        changing — even while the CLI animates spinners in the chrome.

        When the scrollback buffer has truncated old messages (making
        the absolute baseline_count unreachable), falls back to
        locating the sent message text as a landmark.
        """
        lines = capture.split('\n')

        # Trim bottom panel first — separator, prompt, status bar, and
        # conversation picker all live below the content area.  Removing
        # them before bullet-counting prevents the picker's ● (e.g.
        # "● main") from being mistaken for an assistant response bullet.
        content_end = len(lines)
        while content_end > 0:
            s = lines[content_end - 1].strip()
            if (not s or
                    self.RE_SEPARATOR.match(s) or
                    '─' * 10 in s or
                    '❯' in s or
                    self.RE_STATUS.match(s) or
                    self.RE_CONVERSATION_PICKER.match(s) or
                    s.startswith('○') or s.startswith('◯') or
                    'Claude Code' in s or
                    'auto-compact' in s or
                    'bypass permissions' in s or
                    'esc to interrupt' in s or
                    'paste again to expand' in s or
                    'Run /doctor' in s or
                    'Auto-update failed' in s or
                    'context used' in s or
                    'to select' in s or
                    'Enter to view' in s):
                content_end -= 1
            else:
                break

        start_idx = None
        end_idx = content_end

        # Strategy 1: Prompt landmark — find the last ❯ (user input)
        # and extract from the first ● after it.  This is the most
        # reliable strategy because it doesn't depend on absolute
        # bullet counts, which drift when scrollback overflow removes
        # old ● lines between the baseline capture and observation.
        last_prompt_idx = None
        for i in range(content_end - 1, -1, -1):
            if self.RE_USER.match(lines[i]):
                last_prompt_idx = i
                break
        if last_prompt_idx is not None:
            for i in range(last_prompt_idx + 1, content_end):
                if self.RE_RESPONSE.match(lines[i].strip()):
                    start_idx = i
                    break
            if start_idx is not None:
                # Find the LAST ✻ marker — monitor-based responses have
                # multiple sub-turns, each ending with ✻.
                for i in range(start_idx + 1, content_end):
                    if self.RE_COMPLETION.match(lines[i].strip()):
                        end_idx = i + 1

        # Strategy 2: Bullet counting — works when no ❯ is visible
        # (e.g., user input scrolled off in a very long response).
        if start_idx is None:
            bullet_count = 0
            for i, line in enumerate(lines[:content_end]):
                if self.RE_RESPONSE.match(line.strip()):
                    bullet_count += 1
                    if bullet_count == baseline_count + 1 and start_idx is None:
                        start_idx = i

                if start_idx is not None and self.RE_COMPLETION.match(line.strip()):
                    end_idx = i + 1

        # Strategy 3: Needle search — find sent message text as landmark
        # when scrollback truncated too many bullets.  Search from
        # BOTTOM to find the most recent occurrence (channel prompts
        # reuse similar text across turns).
        if start_idx is None and sent_content:
            needle = sent_content.strip().split('\n')[0][:80]
            sent_line_idx = None
            for i in range(content_end - 1, -1, -1):
                if needle and needle in lines[i]:
                    sent_line_idx = i
                    break
            if sent_line_idx is not None:
                for i in range(sent_line_idx + 1, content_end):
                    if self.RE_RESPONSE.match(lines[i].strip()):
                        start_idx = i
                        break
                if start_idx is not None:
                    end_idx = content_end
                    for i in range(start_idx + 1, content_end):
                        if self.RE_COMPLETION.match(lines[i].strip()):
                            end_idx = i + 1

        # Strategy 4: Last-bullet fallback — when scrollback overflow hides
        # the user prompt and baseline bullets.  Only triggers when a NEW ✻
        # marker exists (total completions > baseline_completions), preventing
        # extraction of stale content from previous turns.
        if start_idx is None:
            total_completions = sum(
                1 for ln in lines[:content_end]
                if self.RE_COMPLETION.match(ln.strip()))
            if total_completions > baseline_completions:
                last_bullet_idx = None
                for i in range(content_end - 1, -1, -1):
                    if self.RE_RESPONSE.match(lines[i].strip()):
                        last_bullet_idx = i
                        break
                if last_bullet_idx is not None:
                    start_idx = last_bullet_idx
                    for i in range(start_idx + 1, content_end):
                        if self.RE_COMPLETION.match(lines[i].strip()):
                            end_idx = i + 1

        # Pre-response thinking or compaction: no ● yet but a spinner
        # is visible.  Scan a few lines — compaction puts a progress
        # bar below the spinner line.
        if start_idx is None:
            scanned = 0
            for i in range(content_end - 1, -1, -1):
                s = lines[i].strip()
                if not s:
                    continue
                if self.RE_SPINNER_ACTIVE.match(s) or self.RE_THINKING_SPINNER.match(s):
                    start_idx = i
                    end_idx = content_end
                    break
                scanned += 1
                if scanned >= 3:
                    break

        if start_idx is None:
            return ""

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
