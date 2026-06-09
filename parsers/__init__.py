from .base import TUIParser, TUIMessage, SessionState
from .claude import ClaudeV21Parser
from .registry import select_parser, register, supported_clis

# Backward compat — server.py imports this name
ClaudeTUIParser = ClaudeV21Parser

__all__ = [
    "TUIParser", "TUIMessage", "SessionState",
    "ClaudeTUIParser", "ClaudeV21Parser",
    "select_parser", "register", "supported_clis",
]
