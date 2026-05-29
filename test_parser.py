#!/usr/bin/env python3
"""Unit tests for the Claude TUI parser.

No tmux, no Claude, no network — just regex against captured TUI snapshots.
Run: python3 test_parser.py
"""

import unittest
from parsers.claude import ClaudeV21Parser
from parsers.base import SessionState


class TestDetectState(unittest.TestCase):
    """Test state detection from TUI captures."""

    def setUp(self):
        self.parser = ClaudeV21Parser()

    def test_empty_is_dead(self):
        self.assertEqual(self.parser.detect_state(""), SessionState.DEAD)
        self.assertEqual(self.parser.detect_state("   \n  \n"), SessionState.DEAD)

    def test_idle_with_empty_prompt(self):
        capture = """
❯
────────────────────────────────────────────
  ⏵⏵ bypass permissions on
"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.IDLE)

    def test_idle_with_status_bar(self):
        capture = """
● Here is my response.

✻ 2.3s

❯
────────────────────────────────────────────
  ⏵⏵ bypass permissions on · ← for agents
"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.IDLE)

    def test_idle_after_completion(self):
        capture = """
● Done!

✻ 1.5s
"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.IDLE)

    def test_responding_bullet_no_completion(self):
        capture = """
❯ hello

● I'm currently working on
"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.RESPONDING)

    def test_responding_mid_stream(self):
        capture = """
● Let me check that for you. I'll look at the files and

"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.RESPONDING)

    def test_dialog_enter_to_confirm(self):
        capture = """
 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.DIALOG)

    def test_dialog_esc_to_cancel(self):
        capture = """
Do you want to proceed?
Esc to cancel
"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.DIALOG)

    def test_dialog_selection_marker(self):
        capture = """
❯ 1. Option A
  2. Option B
  3. Option C
"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.DIALOG)

    def test_prompt_with_no_spinner_is_idle(self):
        """Prompt visible with no thinking spinner — can't distinguish from awaiting input."""
        capture = """
❯ what is 2+2?


"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.IDLE)

    def test_active_spinner_is_thinking_not_idle(self):
        """In-progress ✻ with ellipsis must NOT be detected as IDLE."""
        capture = """
● Reading 1 file… (ctrl+o to expand)
  └ .lit/CLAUDE.md

✻ Accomplishing… (3s · ↓ 8 tokens)
────────────────────────────────────────────
❯
  ⏵⏵ bypass permissions on · ← for agents
"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.THINKING)

    def test_completed_spinner_is_idle(self):
        """Completed ✻ without ellipsis is proper IDLE."""
        capture = """
● Here is my response.

✻ Brewed for 9s
────────────────────────────────────────────
❯
  ⏵⏵ bypass permissions on · ← for agents
"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.IDLE)

    def test_completed_duration_only_is_idle(self):
        """Short ✻ duration marker is proper IDLE."""
        capture = """
● Done!

✻ 2.3s
"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.IDLE)


class TestExtractMessages(unittest.TestCase):
    """Test message extraction from TUI captures."""

    def setUp(self):
        self.parser = ClaudeV21Parser()

    def test_single_exchange(self):
        capture = """
❯ hello

● Hi there! How can I help?

✻ 1.2s
"""
        msgs = self.parser.extract_messages(capture)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].role, "user")
        self.assertEqual(msgs[0].content, "hello")
        self.assertEqual(msgs[1].role, "assistant")
        self.assertIn("Hi there", msgs[1].content)
        self.assertEqual(msgs[1].duration, "1.2s")

    def test_multiple_exchanges(self):
        capture = """
❯ what is 2+2?

● 4

✻ 0.8s

───────────────────────────────────

❯ and 3+3?

● 6

✻ 0.5s
"""
        msgs = self.parser.extract_messages(capture)
        self.assertEqual(len(msgs), 4)
        self.assertEqual(msgs[0].content, "what is 2+2?")
        self.assertEqual(msgs[1].content, "4")
        self.assertEqual(msgs[2].content, "and 3+3?")
        self.assertEqual(msgs[3].content, "6")

    def test_multiline_response(self):
        capture = """
❯ explain briefly

● Here are the key points:

  1. First thing
  2. Second thing
  3. Third thing

  That covers it.

✻ 3.1s
"""
        msgs = self.parser.extract_messages(capture)
        self.assertEqual(len(msgs), 2)
        self.assertIn("First thing", msgs[1].content)
        self.assertIn("Third thing", msgs[1].content)
        self.assertIn("That covers it", msgs[1].content)

    def test_incomplete_response_no_completion(self):
        capture = """
❯ tell me a story

● Once upon a time there was a
"""
        msgs = self.parser.extract_messages(capture)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1].role, "assistant")
        self.assertIn("Once upon a time", msgs[1].content)
        self.assertIsNone(msgs[1].duration)

    def test_empty_capture(self):
        msgs = self.parser.extract_messages("")
        self.assertEqual(msgs, [])

    def test_no_messages_just_chrome(self):
        capture = """
╭─── Claude Code v2.1.145 ───╮
│    Welcome!                 │
╰─────────────────────────────╯

❯
────────────────────────────────
  ⏵⏵ bypass permissions on
"""
        msgs = self.parser.extract_messages(capture)
        self.assertEqual(msgs, [])


class TestCountAssistant(unittest.TestCase):

    def setUp(self):
        self.parser = ClaudeV21Parser()

    def test_zero_messages(self):
        self.assertEqual(self.parser.count_assistant_messages("❯ hello"), 0)

    def test_one_response(self):
        capture = "❯ hi\n\n● hello\n\n✻ 1s"
        self.assertEqual(self.parser.count_assistant_messages(capture), 1)

    def test_three_responses(self):
        capture = """
● first
✻ 1s
● second
✻ 1s
● third
✻ 1s
"""
        self.assertEqual(self.parser.count_assistant_messages(capture), 3)


class TestExtractNewResponse(unittest.TestCase):

    def setUp(self):
        self.parser = ClaudeV21Parser()

    def test_new_response_after_baseline(self):
        capture = """
❯ first
● response one
✻ 1s
❯ second
● response two
✻ 1s
"""
        result = self.parser.extract_new_response(1, capture)
        self.assertEqual(result.strip(), "response two")

    def test_no_new_response(self):
        capture = "❯ hi\n● hello\n✻ 1s"
        result = self.parser.extract_new_response(1, capture)
        self.assertEqual(result, "")

    def test_response_still_streaming(self):
        capture = """
❯ hi
● hello there
✻ 1s
❯ tell me more
● Here is some more info about
"""
        result = self.parser.extract_new_response(1, capture)
        self.assertIn("more info", result)


class TestStartupDialogs(unittest.TestCase):

    def setUp(self):
        self.parser = ClaudeV21Parser()

    def test_trust_dialog(self):
        capture = """
 Quick safety check: Is this a project you created?
 ❯ 1. Yes, I trust this folder
   2. No, exit
 Enter to confirm · Esc to cancel
"""
        result = self.parser.is_startup_dialog(capture)
        self.assertIsNotNone(result)
        self.assertEqual(result[2], "workspace-trust")

    def test_bypass_permissions_dialog(self):
        capture = "Do you accept? Yes, I accept the risks"
        result = self.parser.is_startup_dialog(capture)
        self.assertIsNotNone(result)
        self.assertEqual(result[2], "bypass-permissions")
        self.assertEqual(result[1], ["Down", "Enter"])

    def test_theme_dialog(self):
        capture = "Choose the text style for responses"
        result = self.parser.is_startup_dialog(capture)
        self.assertIsNotNone(result)
        self.assertEqual(result[2], "theme-selection")

    def test_no_dialog(self):
        capture = "❯ \n────\n  ⏵⏵ bypass permissions on"
        result = self.parser.is_startup_dialog(capture)
        self.assertIsNone(result)


class TestParse(unittest.TestCase):

    def setUp(self):
        self.parser = ClaudeV21Parser()

    def test_parse_with_version(self):
        capture = """
╭─── Claude Code v2.1.145 ───╮
│    Welcome!                 │
╰─────────────────────────────╯

❯ hello

● Hi!

✻ 0.5s
"""
        tui = self.parser.parse(capture)
        self.assertEqual(tui.version, "v2.1.145")
        self.assertEqual(len(tui.messages), 2)

    def test_parse_with_errors(self):
        capture = """
❯ do something

● Let me try...

✗ Error: file not found

✻ 1.0s
"""
        tui = self.parser.parse(capture)
        self.assertEqual(len(tui.errors), 1)
        self.assertIn("file not found", tui.errors[0])

    def test_parse_no_errors(self):
        capture = "❯ hi\n● hello\n✻ 1s"
        tui = self.parser.parse(capture)
        self.assertEqual(tui.errors, [])


class TestEdgeCases(unittest.TestCase):
    """Edge cases and regressions."""

    def setUp(self):
        self.parser = ClaudeV21Parser()

    def test_scrollback_dialog_doesnt_poison_state(self):
        """The bug we fixed: old dialog text in scrollback caused false DIALOG state."""
        capture = """
❯
────────────────────────────────────────────
  ⏵⏵ bypass permissions on
"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.IDLE)

    def test_status_bar_variations(self):
        for status in [
            "  ⏵⏵ bypass permissions on",
            "  ⏵⏵ bypass permissions on · ← for agents",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
        ]:
            capture = f"❯ \n────\n{status}"
            self.assertEqual(
                self.parser.detect_state(capture), SessionState.IDLE,
                f"Failed for status bar: {status}"
            )

    def test_unicode_in_response(self):
        capture = "❯ test\n\n● Here's some unicode: 你好 🎉 café\n\n✻ 1s"
        msgs = self.parser.extract_messages(capture)
        self.assertEqual(len(msgs), 2)
        self.assertIn("你好", msgs[1].content)
        self.assertIn("🎉", msgs[1].content)

    def test_conversation_picker_doesnt_poison_bullet_count(self):
        """● in conversation picker must not be counted as an assistant bullet."""
        capture = """
❯ plan organic streaming

● Plan(Plan organic streaming)
  ⎿ Read(stream_buffer.py)
    Bash(grep -n "stream_buffer_manager" server.py)
    Running...
  × 32 tool uses (ctrl+o to expand)

✻ Schlepping… (4m 38s · ↓ 6.0k tokens)
────────────────────────────────────────────
❯
  ⏵⏵ bypass permissions on · ← for agents
● main            ↑/↓ to select · Enter to view
○ Plan  Plan organic streaming     4m 31s
"""
        response = self.parser.extract_raw_response(0, capture)
        self.assertNotIn("↑/↓ to select", response)
        self.assertNotIn("○ Plan", response)

    def test_bare_prompt_and_separator_stripped_from_raw_response(self):
        """Bare ❯ prompt and separator must not leak into streamed content."""
        capture = """
❯ hello

● Here is my response so far and it keeps going

────────────────────────────────────────────
❯
  ⏵⏵ bypass permissions on · ← for agents
"""
        response = self.parser.extract_raw_response(0, capture)
        self.assertNotIn('────', response)
        self.assertNotIn('❯', response)
        self.assertIn('Here is my response', response)

    def test_conversation_picker_doesnt_cause_false_responding(self):
        """● in conversation picker must not trigger RESPONDING state."""
        capture = """
● Here is my response.

✻ Brewed for 9s
────────────────────────────────────────────
❯
  ⏵⏵ bypass permissions on · ← for agents
● main            ↑/↓ to select · Enter to view
○ Plan  Plan organic streaming     4m 31s
"""
        self.assertEqual(self.parser.detect_state(capture), SessionState.IDLE)

    def test_response_with_code_block(self):
        capture = """
❯ show code

● Here's the code:

  ```python
  def hello():
      print("hello")
  ```

  That should work.

✻ 2.0s
"""
        msgs = self.parser.extract_messages(capture)
        self.assertEqual(len(msgs), 2)
        self.assertIn("def hello", msgs[1].content)
        self.assertIn("That should work", msgs[1].content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
