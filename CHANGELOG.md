# Changelog

## 0.1.0 — 2026-06-05

Initial public release.

- **Session management**: Create, send, kill, list, status, ping
- **Observe loop**: Polls tmux pane, detects state transitions, extracts response text
- **Completion detection**: Confirmed idle (✻ marker), quiescence, no-progress timeout
- **Tool events**: tool_use/tool_result from JSONL watcher
- **Compaction detection**: Tracks context percentage, auto-redispatches on compaction
- **Session lifecycle**: Survives daemon restarts, adopts existing tmux sessions, idle reaping with resume
- **Multi-session**: One daemon manages concurrent CLI sessions with independent state
- **Socket + stdio modes**: Unix domain socket for production, stdio for testing
- **Parser plugin system**: Version-aware registry, ships with Claude Code 2.1.x parser
- **Zero dependencies**: Python 3.10+ stdlib only
