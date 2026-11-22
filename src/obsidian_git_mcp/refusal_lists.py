"""The configured refusal lists: pinned paths and guard patterns (VAULT-REFUSE-*).

Guard patterns and the text they match are never put in an exception, a log line or a
response: a refusal carries only the number of matches a call would add (VAULT-REFUSE-007).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from obsidian_git_mcp.names import key

if TYPE_CHECKING:
    from obsidian_git_mcp.config import RefusalSource, Settings
    from obsidian_git_mcp.ports import RefusalReader

log = logging.getLogger(__name__)


class SourceUnusable(Exception):
    """A refusal source was read but its content could not be used."""


# @spec VAULT-REFUSE-002
def entries(source: RefusalSource, text: str) -> list[str]:
    """A source's entries, by its configured extraction (VAULT-REFUSE-002)."""
    if source.extraction == "list":
        return [line.strip() for line in text.splitlines() if line.strip()]
    try:
        loaded = YAML(typ="safe", pure=True).load(text)
    except (YAMLError, ValueError, TypeError) as exc:
        raise SourceUnusable from exc
    if loaded is None:
        return []
    if not isinstance(loaded, dict):
        raise SourceUnusable
    found: list[str] = []
    for k, v in loaded.items():
        values = v if isinstance(v, list) else [v]
        for item in [k, *values]:
            if isinstance(item, str) and item.startswith(source.prefix):
                stripped = item[len(source.prefix) :]
                if stripped:
                    found.append(stripped)
    return found


@dataclass
class Lists:
    """The refusal lists as read for one attempt of one changing call."""

    pins: list[str] = field(default_factory=list)  # from readable sources only
    guards: list[re.Pattern[str]] | None = None  # None when the guard source is unreadable
    unavailable: list[str] = field(default_factory=list)  # locations that could not be read

    def pinned(self, path: str) -> bool:
        k = key(path.strip("/"))
        return any(key(p.strip("/")) == k for p in self.pins)

    def matches(self, text: str) -> int:
        """How many times the guard patterns match `text`, searched with no flags (REFUSE-008)."""
        return sum(1 for pattern in self.guards or [] for _ in pattern.finditer(text))


def read(settings: Settings, reader: RefusalReader) -> Lists:
    """Read every configured source now (VAULT-REFUSE-001, -009, -010, VAULT-ID-005)."""
    lists = Lists(guards=[])
    for source in settings.pinned_sources:
        try:
            lists.pins.extend(entries(source, reader.read(source)))
        except (OSError, SourceUnusable):
            log.warning("refusal source unavailable: %s", source.location)
            lists.unavailable.append(source.location)
    if settings.guard_source is not None:
        source = settings.guard_source
        try:
            lists.guards = [re.compile(p) for p in entries(source, reader.read(source))]
        except (OSError, SourceUnusable, re.error):
            # the exception may quote a pattern, so it is dropped, never logged
            log.warning("refusal source unavailable: %s", source.location)
            lists.unavailable.append(source.location)
            lists.guards = None
    return lists
