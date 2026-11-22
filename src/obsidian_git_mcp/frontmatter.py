"""Frontmatter: reading it, and setting fields without disturbing anything else (VAULT-FM-*)."""

from __future__ import annotations

import datetime
import io
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

if TYPE_CHECKING:
    from collections.abc import Mapping

_OPEN = re.compile(r"---[ \t]*\r?\n")
_CLOSE = re.compile(r"^(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.MULTILINE)
_KEY = re.compile(
    r"""^(?:"((?:[^"\\]|\\.)*)"|'((?:[^']|'')*)'|((?!- )[^\s#:"'][^:#\r\n]*?))"""
    r"""[ \t]*:(?=[ \t]|\r?\n|$)"""
)


class FrontmatterUnparsed(Exception):
    """The note's frontmatter is not a YAML mapping."""


@dataclass(frozen=True)
class Split:
    """A note cut into frontmatter and body. `raw` is the YAML text, or None when absent."""

    raw: str | None
    head: str  # everything before the body: delimiters and YAML, or ""
    body: str

    @property
    def raw_start(self) -> int:
        """Offset of the YAML text within the note."""
        return self.head.index("\n") + 1 if self.raw is not None else 0


def split(text: str) -> Split:
    if not _OPEN.match(text):
        return Split(None, "", text)
    start = _OPEN.match(text)
    assert start is not None
    close = _CLOSE.search(text, start.end())
    if close is None:
        return Split(None, "", text)
    return Split(text[start.end() : close.start()], text[: close.end()], text[close.end() :])


def _safe_yaml() -> YAML:
    yaml = YAML(typ="safe", pure=True)
    yaml.allow_duplicate_keys = False
    return yaml


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime.date | datetime.datetime | datetime.time):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def parse(raw: str | None) -> dict[str, Any]:
    """The frontmatter as a dict; {} when absent. Raises FrontmatterUnparsed."""
    if raw is None or not raw.strip():
        return {}
    try:
        loaded = _safe_yaml().load(raw)
    except (YAMLError, ValueError, TypeError) as exc:
        raise FrontmatterUnparsed from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise FrontmatterUnparsed
    result = _jsonable(loaded)
    assert isinstance(result, dict)
    return result


def read(text: str) -> tuple[dict[str, Any] | str, bool, str]:
    """(frontmatter, unparsed, body) as read_note returns them (VAULT-FM-004)."""
    parts = split(text)
    try:
        return parse(parts.raw), False, parts.body
    except FrontmatterUnparsed:
        return parts.raw or "", True, parts.body


def _dump(name: str, value: Any) -> str:
    yaml = YAML(typ="safe", pure=True)
    yaml.default_flow_style = False
    yaml.width = 1_000_000
    yaml.allow_unicode = True
    out = io.StringIO()
    yaml.dump({name: value}, out)
    return out.getvalue()


def _flow_item(value: Any) -> str:
    item = _dump("x", value)[len("x: ") :].rstrip("\n")
    if isinstance(value, str) and item == value and any(c in value for c in ",[]{}"):
        return json.dumps(value, ensure_ascii=False)  # a JSON string is a YAML double-quoted one
    return item


def render_field(name: str, value: Any) -> str:
    """One `key: value` line as YAML, JSON types mapped to YAML types (VAULT-FM-003).

    Lists of scalars are written in flow style (`key: [a, 2, true]`), as notes usually hold them.
    """
    if isinstance(value, list | tuple) and all(
        not isinstance(v, dict | list | tuple) for v in value
    ):
        prefix = _dump(name, None)[: -len("null\n")]
        items = [_flow_item(v) for v in value]
        return prefix + "[" + ", ".join(items) + "]\n"
    return _dump(name, value)


def _spans(raw: str) -> dict[str, tuple[int, int]]:
    """Each top-level key's (start, end) offsets in `raw`, its continuation lines included."""
    lines = raw.splitlines(keepends=True)
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    offsets.append(pos)
    spans: dict[str, tuple[int, int]] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _KEY.match(line)
        if m is None or line[:1] in (" ", "\t", "#"):
            i += 1
            continue
        if m.group(1) is not None:
            name = m.group(1)
        elif m.group(2) is not None:
            name = m.group(2).replace("''", "'")
        else:
            name = m.group(3).strip()
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            indented = nxt[:1] in (" ", "\t") and bool(nxt.strip())
            if indented or nxt.startswith("- ") or nxt.rstrip("\r\n") == "-":
                j += 1
            elif not nxt.strip():
                # a blank line belongs to the key only if indented content follows it
                k = j
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k < len(lines) and lines[k][:1] in (" ", "\t"):
                    j = k
                else:
                    break
            else:
                break
        spans.setdefault(name, (offsets[i], offsets[j]))
        i = j
    return spans


def _same(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def set_fields(text: str, fields: Mapping[str, Any]) -> str:
    """`text` with `fields` set: None removes a key; other keys keep their bytes (FM-001/002).

    Raises FrontmatterUnparsed when the note has frontmatter that does not parse.
    """
    if not fields:
        return text
    parts = split(text)
    current = parse(parts.raw)
    if parts.raw is None:
        added = "".join(render_field(k, v) for k, v in fields.items() if v is not None)
        if not added:
            return text
        return "---\n" + added + "---\n" + text
    raw = parts.raw
    for name, value in fields.items():
        spans = _spans(raw)
        if value is None:
            if name in spans:
                start, end = spans[name]
                raw = raw[:start] + raw[end:]
            continue
        if name in current and name in spans and _same(current[name], _jsonable(value)):
            continue
        line = render_field(name, value)
        if name in spans:
            start, end = spans[name]
            raw = raw[:start] + line + raw[end:]
        else:
            if raw and not raw.endswith("\n"):
                raw += "\n"
            raw += line
    if raw == parts.raw:
        return text
    head = parts.head
    opener = head[: parts.raw_start]
    closer = head[parts.raw_start + len(parts.raw) :]
    return opener + raw + closer + parts.body


def with_fields(body: str, fields: Mapping[str, Any] | None) -> str:
    """A new note's text: frontmatter from `fields` (when any), then `body`."""
    return set_fields(body, fields or {}) if fields else body
