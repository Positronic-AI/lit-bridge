"""Snapshot-based parser validation.

Each capture file is a real (or realistic) tmux capture-pane snapshot
from a specific CLI version. Tests verify that the parser correctly
identifies state, extracts messages, and detects metadata.

To add tests for a new CLI version:
1. Record captures: `tmux capture-pane -p -t lit-<session> > captures/claude_X.Y.Z_<state>.txt`
2. Add test functions below referencing the new captures
3. If tests pass with the existing parser, extend its version range
4. If tests fail, create a new parser version (e.g. v2_2.py)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from parsers.base import SessionState
from parsers.registry import select_parser

CAPTURES = Path(__file__).parent / "captures"


def read_capture(name: str) -> str:
    return (CAPTURES / name).read_text()


class TestClaudeV21StateDetection:

    def setup_method(self):
        self.parser = select_parser("claude-code", "2.1.152")

    def test_idle(self):
        capture = read_capture("claude_2.1.x_idle.txt")
        assert self.parser.detect_state(capture) == SessionState.IDLE

    def test_thinking(self):
        capture = read_capture("claude_2.1.x_thinking.txt")
        assert self.parser.detect_state(capture) == SessionState.THINKING

    def test_responding(self):
        capture = read_capture("claude_2.1.x_responding.txt")
        assert self.parser.detect_state(capture) == SessionState.RESPONDING

    def test_dialog(self):
        capture = read_capture("claude_2.1.x_dialog.txt")
        assert self.parser.detect_state(capture) == SessionState.DIALOG

    def test_empty_is_dead(self):
        assert self.parser.detect_state("") == SessionState.DEAD
        assert self.parser.detect_state("   \n\n  ") == SessionState.DEAD

    def test_tool_use_is_responding(self):
        capture = read_capture("claude_2.1.x_tool_use.txt")
        assert self.parser.detect_state(capture) == SessionState.IDLE


class TestClaudeV21MessageExtraction:

    def setup_method(self):
        self.parser = select_parser("claude-code", "2.1.152")

    def test_idle_messages(self):
        capture = read_capture("claude_2.1.x_idle.txt")
        messages = self.parser.extract_messages(capture)
        users = [m for m in messages if m.role == "user"]
        assistants = [m for m in messages if m.role == "assistant"]
        assert len(users) == 1
        assert len(assistants) == 1
        assert "What is this project?" in users[0].content
        assert "LIT Platform" in assistants[0].content
        assert assistants[0].duration is not None

    def test_thinking_messages(self):
        capture = read_capture("claude_2.1.x_thinking.txt")
        messages = self.parser.extract_messages(capture)
        users = [m for m in messages if m.role == "user"]
        assert len(users) == 2
        assert "Refactor" in users[1].content

    def test_tool_use_messages(self):
        capture = read_capture("claude_2.1.x_tool_use.txt")
        messages = self.parser.extract_messages(capture)
        assistants = [m for m in messages if m.role == "assistant"]
        assert len(assistants) >= 1
        final = assistants[-1]
        assert "monitor.py" in final.content

    def test_dialog_no_messages(self):
        capture = read_capture("claude_2.1.x_dialog.txt")
        messages = self.parser.extract_messages(capture)
        assert len(messages) == 0


class TestClaudeV21VersionDetection:

    def setup_method(self):
        self.parser = select_parser("claude-code", "2.1.152")

    def test_version_extracted(self):
        capture = read_capture("claude_2.1.x_idle.txt")
        state = self.parser.parse(capture)
        assert state.version == "v2.1.152"

    def test_no_version_in_dialog(self):
        capture = read_capture("claude_2.1.x_dialog.txt")
        state = self.parser.parse(capture)
        assert state.version == "v2.1.152"


class TestClaudeV21StartupDialogs:

    def setup_method(self):
        self.parser = select_parser("claude-code", "2.1.152")

    def test_trust_dialog_detected(self):
        capture = read_capture("claude_2.1.x_dialog.txt")
        result = self.parser.is_startup_dialog(capture)
        assert result is not None
        detect, keys, name = result
        assert name == "workspace-trust"

    def test_idle_no_startup_dialog(self):
        capture = read_capture("claude_2.1.x_idle.txt")
        assert self.parser.is_startup_dialog(capture) is None


class TestClaudeV21NewResponse:

    def setup_method(self):
        self.parser = select_parser("claude-code", "2.1.152")

    def test_new_response_after_baseline(self):
        capture = read_capture("claude_2.1.x_idle.txt")
        text = self.parser.extract_new_response(0, capture)
        assert "LIT Platform" in text

    def test_no_new_response_at_baseline(self):
        capture = read_capture("claude_2.1.x_idle.txt")
        count = self.parser.count_assistant_messages(capture)
        text = self.parser.extract_new_response(count, capture)
        assert text == ""


class TestRegistry:

    def test_known_version(self):
        p = select_parser("claude-code", "2.1.152")
        assert p is not None
        assert type(p).__name__ == "ClaudeV21Parser"

    def test_unknown_version_falls_back(self):
        p = select_parser("claude-code", "3.0.0")
        assert p is not None
        assert type(p).__name__ == "ClaudeV21Parser"

    def test_no_version_uses_latest(self):
        p = select_parser("claude-code")
        assert p is not None

    def test_unknown_cli_returns_none(self):
        assert select_parser("codex") is None


if __name__ == "__main__":
    import traceback
    test_classes = [
        TestClaudeV21StateDetection,
        TestClaudeV21MessageExtraction,
        TestClaudeV21VersionDetection,
        TestClaudeV21StartupDialogs,
        TestClaudeV21NewResponse,
        TestRegistry,
    ]
    passed = failed = 0
    for cls in test_classes:
        inst = cls()
        for name in sorted(dir(inst)):
            if not name.startswith("test_"):
                continue
            if hasattr(inst, "setup_method"):
                inst.setup_method()
            try:
                getattr(inst, name)()
                print(f"  PASS {cls.__name__}.{name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL {cls.__name__}.{name}: {e}")
                traceback.print_exc()
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
