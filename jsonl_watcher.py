"""JSONL transcript watcher for Claude Code sessions.

Tails the JSONL file that Claude Code writes for each session,
extracting tool_use/tool_result events and detecting turn completion.
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("lit-bridge")


def cc_project_dir(working_dir: str, config_dir: str = None) -> Path:
    """Derive Claude Code's project directory from a working directory path."""
    slug = re.sub(r'[^a-zA-Z0-9]', '-', working_dir.lstrip('/'))
    base = Path(config_dir) if config_dir else (Path.home() / ".claude")
    return base / "projects" / f"-{slug}"


class JsonlWatcher:
    """Tails a Claude Code JSONL file for tool_use/tool_result events."""

    def __init__(self, project_dir: Path):
        self._project_dir = project_dir
        self._file: Optional[Path] = None
        self._emitted_tool_ids: set = set()
        # Start at end-of-file so we only see NEW entries
        f = self._find_active_jsonl()
        if f and f.exists():
            self._file = f
            self._pos = f.stat().st_size
        else:
            self._pos = 0

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
        self._turn_text_parts: List[str] = []

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
                if isinstance(content_blocks, list):
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
                                self._turn_text_parts.append(text)
                                events.append({
                                    "event": "jsonl_text",
                                    "text": text,
                                })
                if msg.get('stop_reason') == 'end_turn':
                    full_text = '\n\n'.join(self._turn_text_parts)
                    events.append({"event": "turn_complete",
                                   "content": full_text})
                    self._turn_text_parts = []
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

    _CLI_INTERNAL_MSG = re.compile(
        r'<(local-command-\w+|command-name)\b')

    def get_last_user_message(self) -> Optional[str]:
        """Find the most recent user text message written AFTER the last begin_turn().

        Only reads new JSONL entries (after self._pos) to avoid re-emitting
        stale messages from earlier in the conversation.
        """
        f = self._file or self._find_active_jsonl()
        if not f or not f.exists():
            log.info(f"get_last_user_message: no file (file={self._file})")
            return None
        try:
            size = f.stat().st_size
        except OSError:
            return None
        if size <= self._pos:
            log.info(f"get_last_user_message: no new data (pos={self._pos}, size={size})")
            return None
        log.info(f"get_last_user_message: reading {f.name} from pos={self._pos} ({size - self._pos} new bytes)")
        try:
            with open(f, 'r', encoding='utf-8', errors='replace') as fh:
                fh.seek(self._pos)
                new_data = fh.read()
        except OSError as e:
            log.info(f"get_last_user_message: OSError {e}")
            return None
        lines = new_data.strip().split('\n')
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
                stripped = content.strip()
                if self._CLI_INTERNAL_MSG.search(stripped):
                    log.info(f"get_last_user_message: skipping CLI internal msg at user#{user_count}")
                    continue
                log.info(f"get_last_user_message: found at user#{user_count}: {stripped[:80]!r}")
                return stripped
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        text = block.get('text', '').strip()
                        if text and not self._CLI_INTERNAL_MSG.search(text):
                            log.info(f"get_last_user_message: found block at user#{user_count}: {text[:80]!r}")
                            return text
            if user_count >= 3:
                break
        log.info(f"get_last_user_message: no text user message found in {user_count} new entries")
        return None
