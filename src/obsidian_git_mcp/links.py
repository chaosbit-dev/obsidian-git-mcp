"""Links between notes: which text is a link, where it points, and how to rewrite it."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import unquote

from obsidian_git_mcp import frontmatter
from obsidian_git_mcp.names import Index, Resolution, is_note_path, key, name_of
from obsidian_git_mcp.sections import mask_code

_WIKILINK = re.compile(r"(!?)\[\[([^\[\]\r\n]+?)\]\]")
_MDLINK = re.compile(
    r"(!?)\[([^\[\]\r\n]*)\]\((<[^<>\r\n]*>|[^\s()<>]+)((?:\s+\"[^\"\r\n]*\")?)\)"
)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_ENCODE = {c: f"%{ord(c):02X}" for c in " %()<>[]"}


@dataclass(frozen=True)
class Link:
    """One recognised link. `start`/`end` delimit the text to rewrite (the target only)."""

    kind: str  # "wiki" or "md"
    start: int
    end: int
    target: str  # wiki: target text as written; md: the decoded relative path
    rest: str  # wiki: "#heading|alias" tail; md: "#fragment" (raw)
    angle: bool = False  # md: written between < and >
    had_ext: bool = False  # wiki: target written with .md

    @property
    def is_self(self) -> bool:
        return self.kind == "wiki" and not self.target.strip()


def _mask(text: str) -> str:
    """The note with every region that cannot hold a link blanked out, offsets kept."""
    parts = frontmatter.split(text)
    if parts.raw is None:
        return mask_code(text)
    raw_start = parts.raw_start
    head = list(text[: len(parts.head)])
    # frontmatter: only YAML comment lines are excluded; delimiter lines hold no links
    pos = 0
    for line in parts.raw.splitlines(keepends=True):
        if line.lstrip().startswith("#"):
            for i in range(len(line)):
                if line[i] not in "\r\n":
                    head[raw_start + pos + i] = " "
        pos += len(line)
    for i in range(raw_start):
        head[i] = " " if head[i] not in "\r\n" else head[i]
    for i in range(raw_start + len(parts.raw), len(head)):
        head[i] = " " if head[i] not in "\r\n" else head[i]
    return "".join(head) + mask_code(parts.body)


def _escaped(text: str, at: int) -> bool:
    return at > 0 and text[at - 1] == "\\"


# @spec VAULT-LINK-001, VAULT-LINK-002, VAULT-LINK-003
def find_links(text: str) -> list[Link]:
    """Every link in a note, in order (VAULT-LINK-001..003)."""
    masked = _mask(text)
    found: list[Link] = []
    taken = list(masked)
    for m in _WIKILINK.finditer(masked):
        if _escaped(text, m.start()):
            continue
        inner = text[m.start(2) : m.end(2)]
        cut = min((i for i in (inner.find("#"), inner.find("|")) if i >= 0), default=len(inner))
        target = inner[:cut]
        found.append(
            Link(
                "wiki",
                m.start(2),
                m.start(2) + cut,
                target,
                inner[cut:],
                had_ext=is_note_path(target.strip()),
            )
        )
        for i in range(m.start(), m.end()):
            taken[i] = " "
    remaining = "".join(taken)
    for m in _MDLINK.finditer(remaining):
        if _escaped(text, m.start()):
            continue
        url = text[m.start(3) : m.end(3)]
        angle = url.startswith("<")
        raw = url[1:-1] if angle else url
        path, hash_, fragment = raw.partition("#")
        decoded = path if angle else unquote(path)
        if not decoded or decoded.startswith("/") or _SCHEME.match(decoded):
            continue
        if not is_note_path(decoded):
            continue
        found.append(Link("md", m.start(3), m.end(3), decoded, hash_ + fragment, angle=angle))
    found.sort(key=lambda link: link.start)
    return found


def resolve(link: Link, source: str, index: Index) -> Resolution:
    """Where `link`, written in the note at `source`, points within `index`."""
    if link.is_self:
        return Resolution(source, (source,))
    if link.kind == "md":
        base = posixpath.dirname(source)
        joined = posixpath.normpath(posixpath.join(base, link.target))
        if joined == ".." or joined.startswith("../"):
            return Resolution()
        found = index.path(joined)
        return Resolution(found, (found,)) if found else Resolution()
    target = link.target.strip()
    if target.startswith(("./", "../")):
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
        found = index.path(joined)
        return Resolution(found, (found,)) if found else Resolution()
    if "/" in target:
        found = index.path(target.lstrip("/"))
        return Resolution(found, (found,)) if found else Resolution()
    return index.name(target)


def _encode(path: str) -> str:
    return "".join(_ENCODE.get(c, c) for c in path)


def rewrite_target(link: Link, target: str, source: str, index: Index) -> str:
    """The new text for `link`'s target span so it points at `target` (VAULT-LINK-006).

    `source` is the linking note's path and `index` the resolvable notes, both after the move.
    """
    if link.kind == "md":
        rel = posixpath.relpath(target, posixpath.dirname(source) or ".")
        if link.angle:
            return "<" + rel + link.rest + ">"
        return _encode(rel) + link.rest
    name = name_of(target)
    # a name where it reaches the note uniquely, a path where it does not
    written = name if index.name(name).target == target else target[:-3]
    if link.had_ext:
        written += target[-3:]
    return written


def same_note(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and key(a) == key(b)
