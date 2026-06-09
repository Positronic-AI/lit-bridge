"""TUI observer loop — completion detection and response extraction.

The observe loop is the heart of lit-bridge. For each managed session,
a background task polls the tmux pane, detects state changes, extracts
response content, and emits events when the CLI finishes responding.

Completion detection (in priority order):
  1. Confirmed idle: JSONL end_turn or TUI ✻ marker + idle state + debounce
  2. Quiescence: response stopped growing for 5s (confirmed) or 30s (unconfirmed)
  3. No-progress timeout: 90s with no response text (skipped during THINKING)
"""

import asyncio
import logging
import re
import time
from typing import Callable, Optional, TYPE_CHECKING

from parsers import SessionState

if TYPE_CHECKING:
    from server import ManagedSession

log = logging.getLogger("lit-bridge")

POLL_INTERVAL = 0.3
QUIESCENCE_TIMEOUT = 5.0
QUIESCENCE_UNCONFIRMED_TIMEOUT = 30.0
COMPLETION_DEBOUNCE = 2.0
AUTO_OBSERVE_COOLDOWN = 1.5
NO_PROGRESS_TIMEOUT = 90.0
IDLE_REAP_TIMEOUT = 3600.0


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


async def observe_loop(
    ms: 'ManagedSession',
    emit: Callable[[dict], None],
    is_running: Callable[[], bool],
    has_client: Callable[[], bool],
    on_reap: Callable[['ManagedSession'], None],
):
    """Continuous TUI observation for a session.

    Args:
        ms: The managed session to observe.
        emit: Callback to emit events (JSON dicts).
        is_running: Returns False when the monitor is shutting down.
        has_client: Returns True when a client is connected (for logging).
        on_reap: Called when a session is reaped (idle timeout).
    """
    prev_state = ms.state
    last_response_change = time.monotonic()
    last_capture_change = time.monotonic()
    last_observe_complete = time.monotonic()
    idle_confirmed_since = 0.0
    idle_confirmed_content = ""
    prev_capture = ""
    poll_count = 0

    while is_running():
        try:
            await asyncio.sleep(POLL_INTERVAL)

            if not await ms.tmux.is_alive():
                if ms.state != SessionState.DEAD:
                    emit({"session": ms.name, "event": "state",
                          "from": ms.state.value, "to": "dead"})
                    ms.state = SessionState.DEAD
                break

            visible = await ms.tmux.capture_pane(visible_only=True)
            now = time.monotonic()
            poll_count += 1

            if poll_count % 100 == 0:
                log.info(f"[{ms.name}] heartbeat poll={poll_count} "
                         f"state={prev_state.value} observing={ms.observing} "
                         f"client={'yes' if has_client() else 'no'}")

            if visible != prev_capture:
                last_capture_change = now
                prev_capture = visible

            new_state = ms.parser.detect_state(visible)
            transitioned_from_idle = (
                new_state != prev_state and
                prev_state == SessionState.IDLE
            )

            if not ms.observing and poll_count % 10 == 0:
                last_lines = visible.strip().split('\n')[-3:] if visible else []
                log.info(f"[{ms.name}] poll state={new_state.value} "
                         f"bottom={[l.strip()[:60] for l in last_lines]}")

            if new_state != prev_state:
                log.info(f"[{ms.name}] State: {prev_state.value} → {new_state.value} "
                         f"(observing={ms.observing})")

            if new_state != prev_state:
                emit({"session": ms.name, "event": "state",
                      "from": prev_state.value, "to": new_state.value})
                prev_state = new_state
                ms.state = new_state

            if new_state != SessionState.IDLE or (ms.observing and not ms._paused):
                ms._last_active = now

            # Idle reaping
            reapable = (not ms.observing) or (ms._paused and new_state == SessionState.IDLE)
            if (reapable and
                    new_state == SessionState.IDLE and
                    (now - ms._last_active) > IDLE_REAP_TIMEOUT):
                if getattr(ms, '_yielded', '') and ms.channel_id:
                    b_evt = {"session": ms.name, "event": "boundary",
                             "content": ms._yielded,
                             "channel_id": ms.channel_id}
                    if ms.team:
                        b_evt["team"] = ms.team
                    emit(b_evt)
                on_reap(ms)
                break

            # Auto-observe organic interaction
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
                    emit(evt)
                    ms._jsonl_watcher.begin_turn()
                else:
                    log.info(f"[{ms.name}] No JSONL watcher for organic input")
                ms._is_organic = True
                ms.observing = True
                log.info(f"[{ms.name}] Auto-observing organic interaction")

            # Response extraction + completion detection
            if ms.observing and hasattr(ms, '_baseline_count'):
                full_capture = await ms.tmux.capture_pane()
                response = ms.parser.extract_raw_response(
                    ms._baseline_count, full_capture,
                    sent_content=getattr(ms, '_sent_content', None),
                    baseline_completions=getattr(ms, '_baseline_completion_count', 0))
                response = _unwrap_tmux_lines(response, getattr(ms, '_pane_width', 0))

                # ── Turn confirmation (TUI check) ──
                turn_confirmed = getattr(ms, '_turn_confirmed', False)
                last_bullet_line = -1
                baseline_completions = getattr(ms, '_baseline_completion_count', 0)
                for li, ln in enumerate(full_capture.split('\n')):
                    if re.match(r'^\s*●\s', ln) and not ms.parser.RE_CONVERSATION_PICKER.match(ln.strip()):
                        last_bullet_line = li
                if last_bullet_line >= 0:
                    after_lines = full_capture.split('\n')[last_bullet_line:]
                    for ln in after_lines:
                        if re.match(r'^\s*✻\s', ln):
                            total_completions = sum(
                                1 for l in full_capture.split('\n')
                                if re.match(r'^\s*✻\s', l))
                            if total_completions <= baseline_completions:
                                break
                            if re.search(r'✻\s+.*…', ln):
                                turn_confirmed = False
                            elif re.search(r'monitor.*running', ln, re.IGNORECASE):
                                turn_confirmed = False
                            else:
                                turn_confirmed = True
                                ms._turn_confirmed = True
                            break

                # ── Emit response content ──
                if response != ms._yielded:
                    has_bullet = bool(re.search(r'^\s*●\s', response, re.MULTILINE))
                    had_bullet = bool(re.search(r'^\s*●\s', ms._yielded or '', re.MULTILINE))
                    # Don't emit replace events that lose the response bullet —
                    # TUI redraws can temporarily hide scrollback content,
                    # causing the frontend to flicker between content and spinner.
                    if had_bullet and not has_bullet:
                        pass
                    else:
                        evt = {"session": ms.name, "event": "replace",
                               "text": response,
                               "organic": ms._is_organic}
                        if ms.channel_id:
                            evt["channel_id"] = ms.channel_id
                        if ms.team:
                            evt["team"] = ms.team
                        emit(evt)
                    if has_bullet and not getattr(ms, '_jsonl_content_set', False):
                        ms._yielded = response
                        ms._paused = False
                    last_response_change = now

                # ── JSONL watcher ──
                if ms._jsonl_watcher:
                    for tool_evt in ms._jsonl_watcher.poll():
                        if tool_evt.get("event") == "turn_complete":
                            turn_confirmed = True
                            ms._turn_confirmed = True
                            jsonl_content = tool_evt.get("content", "")
                            if jsonl_content:
                                if ms._yielded and ms._jsonl_content_set:
                                    ms._yielded += "\n\n" + jsonl_content
                                else:
                                    ms._yielded = jsonl_content
                                ms._jsonl_content_set = True
                                ms._paused = False
                                last_response_change = now
                            log.info(f"[{ms.name}] JSONL: end_turn — "
                                     f"turn confirmed, {len(jsonl_content)} chars")
                            continue
                        if tool_evt.get("event") in (
                                "tool_use", "tool_result"):
                            continue
                        tool_evt["session"] = ms.name
                        emit(tool_evt)
                        last_response_change = now

                # ── Completion: confirmed idle + debounce ──
                has_active_spinner = any(
                    ms.parser.RE_SPINNER_ACTIVE.match(ln)
                    for ln in (ms._yielded or '').split('\n'))
                has_active_monitor = bool(re.search(
                    r'monitor.*running|\d+\s+monitor', visible, re.IGNORECASE))
                if (new_state == SessionState.IDLE and turn_confirmed
                        and not has_active_spinner
                        and not has_active_monitor
                        and not ms._paused):
                    if idle_confirmed_since == 0.0:
                        idle_confirmed_since = now
                        idle_confirmed_content = ms._yielded
                    elif ms._yielded != idle_confirmed_content:
                        idle_confirmed_since = now
                        idle_confirmed_content = ms._yielded
                    # JSONL fast path: end_turn is authoritative, skip debounce
                    jsonl_confirmed = getattr(ms, '_jsonl_content_set', False)
                    if (now - idle_confirmed_since) >= COMPLETION_DEBOUNCE or jsonl_confirmed:
                        if not ms._yielded:
                            await asyncio.sleep(0.5)
                            full_capture = await ms.tmux.capture_pane()
                            response = ms.parser.extract_raw_response(
                                ms._baseline_count, full_capture,
                                sent_content=getattr(ms, '_sent_content', None),
                                baseline_completions=getattr(ms, '_baseline_completion_count', 0))
                            response = _unwrap_tmux_lines(response, getattr(ms, '_pane_width', 0))
                            if response and re.match(r'^\s*●\s', response):
                                emit({"session": ms.name,
                                      "event": "replace",
                                      "text": response})
                                ms._yielded = response
                        if ms._yielded:
                            compact_pct_now = _parse_compact_pct(visible)
                            if ms.channel_id:
                                evt = {"session": ms.name,
                                       "event": "paused",
                                       "total_length": len(ms._yielded),
                                       "content": ms._yielded,
                                       "organic": ms._is_organic,
                                       "compact_pct_start": getattr(ms, '_compact_pct_start', None),
                                       "compact_pct_end": compact_pct_now,
                                       "channel_id": ms.channel_id}
                                if ms.team:
                                    evt["team"] = ms.team
                                emit(evt)
                                ms._paused = True
                                idle_confirmed_since = 0.0
                                idle_confirmed_content = ""
                                last_observe_complete = now
                                log.info(f"[{ms.name}] Response paused ({len(ms._yielded)} chars)")
                            else:
                                evt = {"session": ms.name,
                                       "event": "complete",
                                       "total_length": len(ms._yielded),
                                       "content": ms._yielded,
                                       "organic": ms._is_organic,
                                       "compact_pct_start": getattr(ms, '_compact_pct_start', None),
                                       "compact_pct_end": compact_pct_now}
                                emit(evt)
                                ms.observing = False
                                idle_confirmed_since = 0.0
                                idle_confirmed_content = ""
                                last_observe_complete = now
                                log.info(f"[{ms.name}] Response complete ({len(ms._yielded)} chars, "
                                         f"compact {getattr(ms, '_compact_pct_start', '?')}%→{compact_pct_now}%)")
                            if ms._jsonl_watcher:
                                meta = ms._jsonl_watcher.get_turn_metadata()
                                if meta:
                                    emit({"session": ms.name,
                                          "event": "metadata", **meta})
                else:
                    idle_confirmed_since = 0.0
                    idle_confirmed_content = ""

                # ── Quiescence fallback ──
                if ms._yielded and ms.observing and not has_active_monitor and not ms._paused:
                    truly_confirmed = turn_confirmed
                    q_timeout = (QUIESCENCE_TIMEOUT if truly_confirmed
                                 else QUIESCENCE_UNCONFIRMED_TIMEOUT)
                    if ((now - last_response_change) > q_timeout and
                            new_state in (SessionState.IDLE,
                                          SessionState.RESPONDING)):
                        if getattr(ms, '_streaming_tool_output', False):
                            emit({"session": ms.name,
                                  "event": "tool_result_done"})
                            ms._streaming_tool_output = False
                        compact_pct_now = _parse_compact_pct(visible)
                        if ms.channel_id:
                            q_evt = {"session": ms.name, "event": "paused",
                                    "total_length": len(ms._yielded),
                                    "content": ms._yielded,
                                    "reason": "quiescence",
                                    "organic": ms._is_organic,
                                    "compact_pct_start": getattr(ms, '_compact_pct_start', None),
                                    "compact_pct_end": compact_pct_now,
                                    "channel_id": ms.channel_id}
                            if ms.team:
                                q_evt["team"] = ms.team
                            emit(q_evt)
                            ms._paused = True
                            last_observe_complete = now + 15.0
                            log.info(f"[{ms.name}] Response paused (quiescence)")
                        else:
                            q_evt = {"session": ms.name, "event": "complete",
                                    "total_length": len(ms._yielded),
                                    "content": ms._yielded,
                                    "reason": "quiescence",
                                    "organic": ms._is_organic,
                                    "compact_pct_start": getattr(ms, '_compact_pct_start', None),
                                    "compact_pct_end": compact_pct_now}
                            emit(q_evt)
                            ms.observing = False
                            last_observe_complete = now + 15.0
                            log.info(f"[{ms.name}] Response complete "
                                     f"(quiescence, confirmed={turn_confirmed}, "
                                     f"compact {getattr(ms, '_compact_pct_start', '?')}%→{compact_pct_now}%)")
                        if ms._jsonl_watcher:
                            meta = ms._jsonl_watcher.get_turn_metadata()
                            if meta:
                                emit({"session": ms.name,
                                      "event": "metadata", **meta})

                # ── No-progress timeout ──
                elif (not ms._yielded and
                        new_state != SessionState.THINKING and
                        (now - last_capture_change) > NO_PROGRESS_TIMEOUT):
                    emit({"session": ms.name, "event": "error",
                          "message": "no progress timeout"})
                    ms.observing = False
                    last_observe_complete = now

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"[{ms.name}] Observer error: {e}", exc_info=True)
            await asyncio.sleep(1.0)
