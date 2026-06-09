# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

lit-bridge is a daemon that manages interactive AI CLI sessions (like Claude Code) inside tmux panes. It speaks a JSON-lines protocol over a Unix socket — the client sends commands, lit-bridge sends back structured events. Zero pip dependencies; Python 3.10+ stdlib only.

The core idea: AI CLIs are interactive TUI programs. Rather than using pipe mode or JSON output (which reclassifies usage as programmatic), lit-bridge runs the CLI in a real terminal and scrapes the TUI to detect state, extract responses, and stream events.

## Running

```bash
# Socket mode (production — survives client disconnects)
python3 monitor.py --socket /tmp/lit-bridge.sock

# Stdio mode (testing — dies with parent)
python3 monitor.py
```

## Tests

```bash
# Parser unit tests (no tmux needed)
python3 tests/test_parser.py

# Snapshot-based parser validation
python3 -m pytest tests/test_parsers.py

# Integration test (needs tmux + claude CLI)
python3 tests/test_monitor.py

# Channel prompt completion test
python3 tests/test_channel_prompt.py
```

## Architecture

```
server.py           # The daemon: session lifecycle, command dispatch, socket/stdio server
observer.py         # TUI observe loop: completion detection, response extraction
jsonl_watcher.py    # JSONL transcript tailing: tool events, turn metadata
tmux_session.py     # Generic tmux wrapper: spawn, capture, send-keys, kill
parsers/
  base.py           # Abstract TUIParser interface + data classes (SessionState, TUIMessage, etc.)
  registry.py       # Version-aware parser selection (maps CLI name → parser class)
  claude/v2_1.py    # Claude Code 2.1.x TUI parser (all regex patterns live here)
```

### Key Classes

- **`Monitor`** (`server.py`): The daemon. Manages `ManagedSession` instances, handles the JSON-lines protocol. One instance per process.
- **`ManagedSession`** (`server.py`): State for one CLI session — the `TmuxSession`, parser, observe task, JSONL watcher, yielded content, and observation flags.
- **`TmuxSession`** (`tmux_session.py`): Thin async wrapper around tmux commands. No business logic.
- **`JsonlWatcher`** (`jsonl_watcher.py`): Tails Claude Code's JSONL transcript file for tool_use/tool_result events and session metadata. Reads from the project-specific directory derived from `CLAUDE_CONFIG_DIR` + working directory.

### The Observe Loop (`observer.py`)

This is the heart of the system (~300 lines). For each managed session, a background task:

1. **Polls** the tmux pane every `POLL_INTERVAL` (0.3s)
2. **Detects state** via the parser (idle, thinking, responding, dialog, dead)
3. **Extracts response text** from the full scrollback capture, comparing against a baseline message count
4. **Emits events**: `state` changes, `replace` (full response content updated), `complete` (turn finished), `tool_use`/`tool_result` (from JSONL watcher)
5. **Handles completion detection**: confirmed idle (✻ marker + prompt visible + debounce), quiescence (content stopped growing), no-progress timeout
6. **Idle reaping**: kills sessions idle for `IDLE_REAP_TIMEOUT` (1 hour), stores session ID for `--resume` on next create

### Session Key Scheme

Sessions are keyed as `{session_name}` for standalone or `{session_name}:{channel_id}` for channel-bound sessions. Multiple channels share a tmux session (separate windows). Window labels follow `{team}:{channel_id}` format.

### Completion Detection

This is the trickiest part of the codebase. Three mechanisms, in priority order:

1. **Confirmed idle**: Parser finds a `✻` marker (e.g. "✻ Brewed for 9s") after the last `●` response block, the `❯` prompt is visible, no active spinners or monitors, and this state is stable for `COMPLETION_DEBOUNCE` (2s).
2. **Quiescence**: Response content stopped growing for `QUIESCENCE_TIMEOUT` (5s if confirmed) or `QUIESCENCE_UNCONFIRMED_TIMEOUT` (30s if not). Skipped entirely when monitors are active.
3. **No-progress timeout**: No response text appeared within `NO_PROGRESS_TIMEOUT` (90s) after send — but not while state is THINKING (context loading can take 60s+).

Active monitors (detected by "monitor...running" in the ✻ line or "N monitor" in the status bar) suppress both confirmed-idle and quiescence completion, since the CLI is pausing between monitor events, not finished.

### Resume After Reap

When a session is reaped (idle >1 hour), the CLI's session ID is captured from the JSONL watcher and stored in `_reaped_sessions`. On the next `create`, `--resume <id>` is appended to the CLI args. If resume fails (session dies during startup), the bridge automatically retries without `--resume`.

The JSONL watcher derives its project directory from `CLAUDE_CONFIG_DIR` (passed via env vars) + working directory. If `CLAUDE_CONFIG_DIR` isn't set, it falls back to `~/.claude/projects/`.

## Parser Development

When Anthropic updates the Claude Code TUI:

1. Record captures: `tmux capture-pane -p -t <session> > tests/captures/claude_X.Y.Z_<state>.txt`
2. Add snapshot tests in `tests/test_parsers.py`
3. If existing parser handles the changes, extend its version range in `registry.py`
4. If not, copy `parsers/claude/v2_1.py` → `v2_2.py`, update patterns, register the new version range

Key regex patterns in the parser (all must match at **line start** to avoid false positives from response prose):
- `RE_USER` (`❯`): User input prompt
- `RE_RESPONSE` (`●`): Assistant response block start
- `RE_COMPLETION` (`✻`): Turn completion marker (e.g. "✻ 2.3s")
- `RE_SPINNER_ACTIVE` (`[·✢-✿]...…`): Active spinner (trailing `…` = still running)
- `RE_CONVERSATION_PICKER` (`●/○ ...`): Session picker overlay (must be stripped before state detection)

## Conventions

- **No pip dependencies.** Everything is stdlib. This is intentional — lit-bridge runs as a sidecar daemon and must be trivially deployable.
- **No LIT dependencies.** `monitor.py` and the parsers know nothing about the LIT platform. The integration layer lives in `lit-lib/src/lit/mux/backends/claude_interactive.py`.
- **Async throughout.** All I/O is async (`asyncio.create_subprocess_shell` for tmux commands). The observe loop and command handler run as concurrent tasks.
- **Two capture modes**: `visible_only=True` (visible screen, for state detection — avoids stale dialog text in scrollback) vs `visible_only=False` (full scrollback, for message extraction).
