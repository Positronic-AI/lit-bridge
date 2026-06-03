# lit-bridge

A lightweight daemon that manages interactive AI CLI sessions in tmux and streams structured events to your application.

**Zero dependencies.** Python 3.10+ stdlib only. No pip packages.

## What it does

lit-bridge sits between your application and an AI CLI (like Claude Code). It:

1. Spawns CLI sessions inside tmux panes
2. Sends messages by typing into the terminal
3. Watches the TUI output to detect state changes (thinking, responding, idle)
4. Extracts response content from the terminal capture
5. Streams structured JSON events back to your application

Your application speaks a simple JSON-lines protocol over a Unix socket. It never touches tmux, never parses terminal output, never manages processes. lit-bridge handles all of that.

## Why

AI CLIs are interactive tools designed for humans. They render rich TUIs with spinners, tool call animations, progress bars, and context management (compaction, resumption). Using `-p` pipe mode or `--output-format json` strips all of that away and reclassifies your usage as programmatic.

lit-bridge preserves the interactive nature of the session. The CLI runs in a real terminal. It manages its own context window, its own tool permissions, its own MCP servers. Your application just sends messages and receives responses — through the front door, not a pipe.

## Quick start

```bash
# Start the daemon
python3 monitor.py --socket /tmp/lit-bridge.sock

# In another terminal, connect and interact:
# (using socat for demo — your app would use a proper socket client)

# Create a session
echo '{"cmd":"create","session":"demo","cli":"claude","parser":"claude-code","args":["--model","sonnet"]}' \
  | socat - UNIX-CONNECT:/tmp/lit-bridge.sock

# Send a message
echo '{"cmd":"send","session":"demo","content":"What is the capital of France?"}' \
  | socat - UNIX-CONNECT:/tmp/lit-bridge.sock

# Events stream back as JSON lines:
# {"session":"demo","event":"state","from":"idle","to":"thinking"}
# {"session":"demo","event":"replace","text":"● The capital of France is Paris."}
# {"session":"demo","event":"complete","content":"● The capital of France is Paris.","total_length":42}
```

## Protocol

lit-bridge speaks JSON-lines. One JSON object per line, newline-delimited, over a Unix domain socket.

### Commands (client → lit-bridge)

#### `create` — Start a new CLI session

```json
{
  "cmd": "create",
  "session": "my-session",
  "cli": "claude",
  "parser": "claude-code",
  "args": ["--model", "opus", "--dangerously-skip-permissions"],
  "working_dir": "/path/to/project",
  "env": {"ANTHROPIC_API_KEY": "sk-..."},
  "channel_id": "optional-channel-name",
  "team": "optional-team-name"
}
```

| Field | Required | Description |
|---|---|---|
| `session` | yes | Unique session name. Used as the tmux session name. |
| `cli` | no | CLI binary name. Default: `"claude"`. |
| `parser` | no | TUI parser to use. Default: `"claude-code"`. |
| `args` | no | Additional CLI arguments. |
| `working_dir` | no | Working directory for the CLI process. |
| `env` | no | Environment variables to set. |
| `channel_id` | no | Channel identifier — creates a separate tmux window within the session. |
| `team` | no | Team identifier — used in window labels. |

Response: `{"session": "my-session", "event": "ready"}` or `{"session": "my-session", "event": "ready", "reused": true}` if adopting an existing tmux window.

#### `send` — Send a message and observe the response

```json
{"cmd": "send", "session": "my-session", "content": "Explain quicksort"}
```

This types the message into the CLI's terminal input and begins observing for a response. Events stream back as the CLI responds (see Events below).

#### `input` — Send text without observing

```json
{"cmd": "input", "session": "my-session", "content": "/model sonnet"}
```

Types text into the terminal without triggering response observation. Useful for slash commands or configuration changes.

#### `keystroke` — Send raw keystrokes

```json
{"cmd": "keystroke", "session": "my-session", "keys": ["Down", "Enter"]}
```

Sends tmux key sequences. Useful for navigating dialogs or menus.

#### `kill` — Terminate a session

```json
{"cmd": "kill", "session": "my-session"}
```

#### `list` — List all sessions

```json
{"cmd": "list"}
```

Response:
```json
{
  "event": "sessions",
  "sessions": [
    {"name": "my-session", "state": "idle", "alive": true, "message_count": 5}
  ]
}
```

#### `status` — Get detailed session status

```json
{"cmd": "status", "session": "my-session"}
```

#### `ping` — Health check

```json
{"cmd": "ping"}
```

Response: `{"event": "pong"}`

### Events (lit-bridge → client)

#### `state` — CLI state changed

```json
{"session": "my-session", "event": "state", "from": "idle", "to": "thinking"}
```

States: `starting`, `idle`, `thinking`, `responding`, `dialog`, `dead`

#### `replace` — Response content updated

```json
{"session": "my-session", "event": "replace", "text": "● The response so far..."}
```

Emitted periodically as the CLI generates output. The `text` field contains the **complete** current response — your application should replace (not append) its display. This model handles tool calls, retries, and edits cleanly.

#### `complete` — Response finished

```json
{
  "session": "my-session",
  "event": "complete",
  "content": "● The full response text",
  "total_length": 142,
  "compact_pct_start": 45,
  "compact_pct_end": 78
}
```

The `compact_pct_start`/`compact_pct_end` fields track context compaction. If compaction occurred (start was low, end jumped), the CLI cleared its context and continued — your application may want to handle this (e.g., re-dispatch a continuation).

#### `tool_use` — CLI invoked a tool

```json
{"session": "my-session", "event": "tool_use", "tool": "Bash", "input": {"command": "ls"}}
```

#### `tool_result` — Tool returned a result

```json
{"session": "my-session", "event": "tool_result", "tool_use_id": "toolu_...", "stdout_preview": "file1.txt\nfile2.txt"}
```

#### `error` — Something went wrong

```json
{"session": "my-session", "event": "error", "message": "session not found"}
```

## Architecture

```
┌─────────────┐     JSON-lines      ┌─────────────┐     tmux      ┌───────────┐
│  Your App   │◄────────────────────►│   lit-bridge    │◄────────────►│ Claude CLI│
│             │   Unix socket        │   daemon     │   capture +  │  (in tmux)│
└─────────────┘                      └─────────────┘   send-keys   └───────────┘
```

- **Your application** connects to the Unix socket and sends commands / receives events.
- **lit-bridge** manages tmux sessions, polls the terminal capture, detects state transitions, extracts response text, and emits structured events.
- **The CLI** runs in a real tmux pane with a real terminal. It doesn't know it's being monitored.

### Multi-session support

One lit-bridge daemon manages multiple sessions. Each session is a separate tmux window. Sessions can be grouped by `channel_id` — multiple channels share a tmux session (separate windows) but have independent state tracking and event routing.

### Session lifecycle

Sessions survive lit-bridge restarts. When the daemon starts, it discovers existing tmux sessions and adopts them. Idle sessions are reaped after 1 hour and can be resumed on the next `create` (the CLI's `--resume` flag restores the conversation).

### Parsers

lit-bridge is CLI-agnostic. All TUI-specific logic lives in parser plugins:

```
parsers/
├── base.py              # Abstract TUIParser interface
├── registry.py          # Version-aware parser selection
└── claude/
    ├── __init__.py
    └── v2_1.py          # Claude Code 2.1.x parser
```

To support a new CLI, implement the `TUIParser` interface:

```python
class TUIParser(ABC):
    def detect_state(self, capture: str) -> SessionState: ...
    def extract_messages(self, capture: str) -> List[TUIMessage]: ...
    def extract_content_blocks(self, capture: str) -> List[TUIContentBlock]: ...
    def count_assistant_messages(self, capture: str) -> int: ...
    def extract_new_response(self, baseline_count: int, capture: str) -> str: ...
    def is_startup_dialog(self, capture: str) -> Optional[Tuple]: ...
    def parse(self, capture: str) -> TUIState: ...
```

Register it in `parsers/registry.py` and pass the parser name in the `create` command.

## Modes

**Socket mode** (recommended): lit-bridge listens on a Unix domain socket and runs as a daemon. Survives client disconnects and reconnects. Buffers events while no client is connected.

```bash
python3 monitor.py --socket /tmp/lit-bridge.sock
```

**Stdio mode**: Reads commands from stdin, writes events to stdout. Dies when the parent process exits. Useful for testing.

```bash
echo '{"cmd":"ping"}' | python3 monitor.py
```

## Requirements

- Python 3.10+
- tmux (any recent version)
- A supported CLI installed and on PATH (e.g., `claude` from `@anthropic-ai/claude-code`)

## License

Proprietary. Copyright Positronic AI.
