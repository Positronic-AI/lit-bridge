"""Version-aware parser registry.

Maps CLI names to their versioned parsers. When a session reports its
CLI version (extracted from the TUI header), the registry selects the
best-matching parser. Unknown versions fall back to the newest parser
with a logged warning.
"""

import logging
import re
from typing import Dict, List, Optional, Tuple, Type

from .base import TUIParser

log = logging.getLogger("tether")

VersionRange = Tuple[str, Optional[str]]  # (min_version, max_version_exclusive)


class ParserEntry:
    __slots__ = ("parser_cls", "min_ver", "max_ver")

    def __init__(self, parser_cls: Type[TUIParser],
                 min_ver: Tuple[int, ...],
                 max_ver: Optional[Tuple[int, ...]]):
        self.parser_cls = parser_cls
        self.min_ver = min_ver
        self.max_ver = max_ver

    def matches(self, version: Tuple[int, ...]) -> bool:
        if version < self.min_ver:
            return False
        if self.max_ver and version >= self.max_ver:
            return False
        return True


def _parse_ver(s: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r'\d+', s)[:3])


_REGISTRY: Dict[str, List[ParserEntry]] = {}
_FALLBACKS: Dict[str, Type[TUIParser]] = {}


def register(cli_name: str, parser_cls: Type[TUIParser],
             min_version: str, max_version: Optional[str] = None):
    entry = ParserEntry(
        parser_cls,
        _parse_ver(min_version),
        _parse_ver(max_version) if max_version else None,
    )
    _REGISTRY.setdefault(cli_name, []).append(entry)
    existing = _FALLBACKS.get(cli_name)
    if existing is None or entry.min_ver >= _REGISTRY[cli_name][-1].min_ver:
        _FALLBACKS[cli_name] = parser_cls


def select_parser(cli_name: str,
                  version: Optional[str] = None) -> Optional[TUIParser]:
    entries = _REGISTRY.get(cli_name)
    if not entries:
        return None

    if version:
        ver = _parse_ver(version)
        for entry in entries:
            if entry.matches(ver):
                return entry.parser_cls()
        fallback = _FALLBACKS.get(cli_name)
        if fallback:
            log.warning(f"No parser validated for {cli_name} v{version}, "
                        f"falling back to {fallback.__name__}")
            return fallback()
        return None

    fallback = _FALLBACKS.get(cli_name)
    return fallback() if fallback else None


def supported_clis() -> List[str]:
    return list(_REGISTRY.keys())


# --- Register known parsers ---
from .claude import ClaudeV21Parser  # noqa: E402

register("claude-code", ClaudeV21Parser, "2.1.0")
