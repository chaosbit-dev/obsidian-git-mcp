"""Names, paths and how a note argument resolves (VAULT-NAME-*)."""

from __future__ import annotations

import posixpath
import unicodedata
from dataclasses import dataclass, field

from obsidian_git_mcp.errors import VaultError

UNLINKABLE = frozenset("[]#^|:")


def key(text: str) -> str:
    """The equivalence key: NFC normalisation, then case folding."""
    return unicodedata.normalize("NFC", text).casefold()


def is_note_path(path: str) -> bool:
    return key(path).endswith(".md")


def name_of(path: str) -> str:
    """A note's name: its file name without the `.md` extension."""
    base = posixpath.basename(path)
    return base[:-3] if is_note_path(base) else base


def depth(path: str) -> int:
    return path.count("/")


def is_hidden(path: str) -> bool:
    """True when any component starts with a dot (a dot-directory or dot-file)."""
    return any(part.startswith(".") for part in path.split("/"))


def under(path: str, folder: str) -> bool:
    """True when `path` is `folder` itself or lies beneath it, compared by equivalence."""
    folder = folder.strip("/")
    if not folder:
        return True
    k, f = key(path), key(folder)
    return k == f or k.startswith(f + "/")


def looks_like_path(arg: str) -> bool:
    """A note argument naming a path rather than a bare name."""
    return "/" in arg or "\\" in arg or is_note_path(arg)


def confine(path: str, *, what: str = "path") -> str:
    """Check a vault-relative path a changing call names (VAULT-WRITE-007); return it cleaned.

    Refuses absolute paths, `..`, empty or dot components, and a name not ending `.md`.
    Symlinks and the configured subdirectory are checked against the tree by the caller:
    paths here are already relative to the subdirectory, so `..` is the only way out of it.
    """
    detail = {what: path}
    if not path or path.startswith("/") or "\\" in path or "\0" in path:
        raise VaultError("confinement", {**detail, "reason": "not a vault-relative path"})
    parts = path.split("/")
    if any(p == ".." for p in parts):
        raise VaultError("confinement", {**detail, "reason": "contains '..'"})
    if any(p == "" for p in parts):
        raise VaultError("confinement", {**detail, "reason": "empty path component"})
    if any(p.startswith(".") for p in parts):
        raise VaultError("confinement", {**detail, "reason": "inside a dot-directory"})
    if not is_note_path(parts[-1]):
        raise VaultError("confinement", {**detail, "reason": "does not end .md"})
    return path


def check_linkable(path: str, *, what: str = "path") -> None:
    """A new note's name must be reachable by a wikilink (VAULT-NAME-007)."""
    name = name_of(path)
    if any(c in UNLINKABLE for c in name):
        raise VaultError(
            "confinement",
            {what: path, "reason": "name contains one of [ ] # ^ | : and no link can reach it"},
        )
    if name != name.strip() or not name:
        raise VaultError(
            "confinement", {what: path, "reason": "name begins or ends with whitespace"}
        )


@dataclass
class Resolution:
    """Where a reference points: one note, several (ambiguous), or none."""

    target: str | None = None
    candidates: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


@dataclass
class Index:
    """The resolvable notes of one tree, keyed for Obsidian-style lookup."""

    paths: list[str]
    by_path: dict[str, str] = field(init=False)
    by_name: dict[str, list[str]] = field(init=False)

    def __post_init__(self) -> None:
        self.by_path = {}
        self.by_name = {}
        for p in self.paths:
            self.by_path[key(p)] = p
            self.by_name.setdefault(key(name_of(p)), []).append(p)

    def path(self, path: str) -> str | None:
        """The note at a path equivalent to `path`, trying `.md` when it is missing."""
        found = self.by_path.get(key(path))
        if found is None and not is_note_path(path):
            found = self.by_path.get(key(path + ".md"))
        return found

    def name(self, name: str) -> Resolution:
        """The shortest-path note with an equivalent name (VAULT-NAME-002/003)."""
        if is_note_path(name):
            name = name[:-3]
        found = self.by_name.get(key(name), [])
        if not found:
            return Resolution()
        shallowest = min(depth(p) for p in found)
        best = sorted(p for p in found if depth(p) == shallowest)
        if len(best) == 1:
            return Resolution(best[0], (best[0],))
        return Resolution(None, tuple(best))

    def resolve(self, arg: str) -> Resolution:
        """Resolve a tool argument: an exact path wins, otherwise a name (no aliases)."""
        found = self.path(arg)
        if found is not None:
            return Resolution(found, (found,))
        if "/" in arg:
            return Resolution()
        return self.name(arg)


def resolve_or_fail(index: Index, arg: str) -> str:
    """The note `arg` names, or VaultError not_found / ambiguous (VAULT-NAME-001..005)."""
    res = index.resolve(arg)
    if res.ambiguous:
        raise VaultError("ambiguous", {"note": arg, "candidates": list(res.candidates)})
    if res.target is None:
        raise VaultError("not_found", {"note": arg})
    return res.target
