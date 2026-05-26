"""Abstract TUI parser interface.

Each CLI (Claude Code, Codex, Gemini) gets its own parser subclass.
The Monitor core is CLI-agnostic — it calls these methods and emits
uniform events regardless of which CLI is underneath.
"""

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


class SessionState(enum.Enum):
    STARTING = "starting"
    DIALOG = "dialog"
    IDLE = "idle"
    THINKING = "thinking"
    RESPONDING = "responding"
    DEAD = "dead"


@dataclass
class TUIMessage:
    role: str        # "user" or "assistant"
    content: str
    duration: Optional[str] = None


@dataclass
class TUIState:
    state: SessionState
    messages: List[TUIMessage] = field(default_factory=list)
    version: Optional[str] = None
    model: Optional[str] = None
    errors: List[str] = field(default_factory=list)


class TUIParser(ABC):
    """Base class for CLI TUI parsers."""

    @abstractmethod
    def detect_state(self, capture: str) -> SessionState:
        """Classify the current TUI state from a capture-pane snapshot."""

    @abstractmethod
    def extract_messages(self, capture: str) -> List[TUIMessage]:
        """Extract all user/assistant message pairs from the capture."""

    @abstractmethod
    def count_assistant_messages(self, capture: str) -> int:
        """Count assistant response blocks in the capture."""

    @abstractmethod
    def extract_new_response(self, baseline_count: int, capture: str) -> str:
        """Get the latest assistant response if one appeared after baseline_count."""

    @abstractmethod
    def is_startup_dialog(self, capture: str) -> Optional[Tuple[str, List[str], str]]:
        """Check if the capture shows a known startup dialog.

        Returns (detect_string, keys_to_send, dialog_name) or None.
        """

    @abstractmethod
    def parse(self, capture: str) -> TUIState:
        """Full parse: state + messages + metadata."""
