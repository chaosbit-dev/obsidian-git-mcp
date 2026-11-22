"""Markdown structure: code and comment masking, headings, and sections (VAULT-SECT-*)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from obsidian_git_mcp.errors import VaultError

_FENCE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^[ ]{0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_CLOSING_HASHES = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)


def _blank(text: str) -> str:
    """Same length, newlines kept, everything else a space."""
    return "".join(c if c in "\r\n" else " " for c in text)


def fenced_lines(text: str) -> list[tuple[int, int, bool]]:
    """Each line's (start, end, inside_fence), fence delimiter lines counted as inside."""
    out: list[tuple[int, int, bool]] = []
    pos = 0
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        start, end = pos, pos + len(line)
        pos = end
        bare = line.rstrip("\r\n")
        m = _FENCE.match(bare)
        if fence is None:
            if m:
                fence = m.group(1)
                out.append((start, end, True))
            else:
                out.append((start, end, False))
        else:
            out.append((start, end, True))
            closes = m is not None and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence)
            if closes and not bare.strip().lstrip(fence[0]):
                fence = None
    return out


def mask_code(text: str) -> str:
    """`text` with fenced blocks, HTML comments and inline code blanked out (VAULT-LINK-003)."""
    chars = list(text)
    for start, end, fenced in fenced_lines(text):
        if fenced:
            chars[start:end] = _blank(text[start:end])
    masked = "".join(chars)
    masked = _COMMENT.sub(lambda m: _blank(m.group(0)), masked)
    masked = _INLINE_CODE.sub(lambda m: _blank(m.group(0)), masked)
    return masked


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    start: int  # offset of the heading line
    content: int  # offset just after the heading line


def headings(body: str) -> list[Heading]:
    """ATX headings outside fenced code blocks."""
    found: list[Heading] = []
    for start, end, fenced in fenced_lines(body):
        if fenced:
            continue
        m = _HEADING.match(body[start:end].rstrip("\r\n"))
        if not m:
            continue
        text = _CLOSING_HASHES.sub("", m.group(2) or "").strip()
        found.append(Heading(len(m.group(1)), text, start, end))
    return found


def section(body: str, heading: str) -> tuple[int, int]:
    """(content start, content end) of the section headed exactly `heading` (VAULT-SECT-001/002).

    Raises not_found or ambiguous (VAULT-SECT-003).
    """
    all_headings = headings(body)
    matches = [h for h in all_headings if h.text == heading]
    if not matches:
        raise VaultError("not_found", {"heading": heading})
    if len(matches) > 1:
        raise VaultError("ambiguous", {"heading": heading, "matches": len(matches)})
    target = matches[0]
    end = len(body)
    for h in all_headings:
        if h.start > target.start and h.level <= target.level:
            end = h.start
            break
    return target.content, end


def _line(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def append(body: str, text: str, heading: str | None) -> str:
    """`body` with `text` added at its end, or at the end of the named section (VAULT-TOOL-003)."""
    addition = _line(text)
    if heading is None:
        if body and not body.endswith("\n"):
            body += "\n"
        return body + addition
    start, end = section(body, heading)
    content = body[start:end]
    kept = content.rstrip("\r\n \t")
    insert = start + len(kept)
    if kept:
        # after the last non-blank line, keeping the blank lines before the next heading
        nl = body.find("\n", insert)
        insert = len(body) if nl == -1 or nl >= end else nl + 1
    prefix = body[:insert]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + addition + body[insert:]


def replace(body: str, text: str, heading: str) -> str:
    """`body` with the named section's content replaced by `text` (VAULT-TOOL-004)."""
    start, end = section(body, heading)
    new = _line(text) if text else ""
    if end < len(body) and new and not new.endswith("\n\n"):
        new += "\n"
    prefix = body[:start]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + new + body[end:]
