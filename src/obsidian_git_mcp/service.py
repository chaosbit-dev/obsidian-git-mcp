"""The vault: one git clone, serialised tool calls, and the write rules."""

from __future__ import annotations

import datetime
import json
import logging
import os
import posixpath
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from obsidian_git_mcp import frontmatter, links, refusal_lists, sections
from obsidian_git_mcp.errors import StartupError, VaultError
from obsidian_git_mcp.git import GitError, Repo, TreeEntry, run
from obsidian_git_mcp.names import (
    Index,
    check_linkable,
    confine,
    is_hidden,
    is_note_path,
    key,
    looks_like_path,
    name_of,
    resolve_or_fail,
    under,
)

if TYPE_CHECKING:
    from obsidian_git_mcp.config import Identity, Settings
    from obsidian_git_mcp.ports import RefusalReader, TokenSource

AUDIT_LOGGER = "obsidian_git_mcp.audit"

Fields = Mapping[str, Any]
Result = dict[str, Any]
_Outcome = tuple[Result, list[str], int, str | None]

log = logging.getLogger(__name__)
audit_log = logging.getLogger(AUDIT_LOGGER)

# Every commit carries the caller as author and the server (the GitHub App) as committer.
COMMITTER = ("obsidian-git-mcp", "obsidian-git-mcp@users.noreply.github.com")
CALLER_TRAILER = "Vault-Caller"
SEARCH_CAP = 100
LIST_CAP = 500
_PUSH_REJECTED = ("[rejected]", "non-fast-forward", "fetch first", "stale info")
_VERBS = {
    "create_note": "create",
    "append_to_note": "append",
    "edit_note": "edit",
    "move_note": "move",
    "delete_note": "delete",
    "archive_note": "archive",
}


def _text(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def _bytes(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def _clean(text: str) -> str:
    """Text safe to put in a response, whatever bytes the note held."""
    return _bytes(text).decode("utf-8", "replace")


class Snapshot:
    """The vault as the clone holds it at one commit."""

    def __init__(self, repo: Repo, settings: Settings) -> None:
        self.repo = repo
        self.commit = repo.head()
        sub = settings.subdirectory.strip("/")
        self.prefix = sub + "/" if sub else ""
        self.trash = settings.trash_folder.strip("/")
        self.entries: dict[str, TreeEntry] = {}
        for entry in repo.tree(self.commit):
            if entry.path.startswith(self.prefix):
                self.entries[entry.path[len(self.prefix) :]] = entry
        self.by_key = {key(p): p for p in self.entries}
        self.notes = sorted(
            p
            for p, e in self.entries.items()
            if e.is_file and is_note_path(p) and not is_hidden(p)
        )
        self.resolvable = [p for p in self.notes if not under(p, self.trash)]
        self.index = Index(self.resolvable)
        self.dirs: dict[str, str] = {}
        for p in self.entries:
            parts = p.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                self.dirs.setdefault(key(d), d)
        self._texts: dict[str, str] = {}

    def repo_path(self, path: str) -> str:
        return self.prefix + path

    def oid(self, path: str) -> str:
        return self.entries[path].oid

    def load(self, paths: list[str]) -> None:
        missing = [p for p in paths if p not in self._texts]
        blobs = self.repo.blobs(self.entries[p].oid for p in missing)
        for p in missing:
            self._texts[p] = _text(blobs[self.entries[p].oid])

    def text(self, path: str) -> str:
        if path not in self._texts:
            self.load([path])
        return self._texts[path]

    def symlink_on(self, path: str) -> str | None:
        """The symlink entry `path` is, or passes through, if any."""
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            found = self.by_key.get(key("/".join(parts[:i])))
            if found is not None and self.entries[found].is_symlink:
                return found
        return None


@dataclass
class Plan:
    """What a changing call would do to the vault, before it is committed."""

    tool: str
    target: str
    files: dict[str, str | None] = field(default_factory=dict)  # vault path -> text; None deletes
    source: str | None = None  # the note moved away from, for move/delete/archive
    also_changed: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class VaultService:
    """Every tool is a method taking the caller's identity first.

    Every result dict carries `commit`: the commit it was answered from (read) or
    made (write; absent when a write changed nothing, with `changed: False` and
    `answered_from` naming the commit the call was answered from).
    Failures raise VaultError.
    """

    def __init__(
        self,
        settings: Settings,
        tokens: TokenSource,
        refusals: RefusalReader,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.tokens = tokens
        self.refusals = refusals
        self.clock = clock
        self.repo = Repo(settings.workdir)
        self._lock = threading.Lock()
        self._last_fetch: float | None = None
        self._failing_since: float | None = None

    # lifecycle ---------------------------------------------------------------
    # @spec VAULT-CLONE-001, VAULT-CLONE-002
    def start(self) -> None:
        """Wipe the workdir and clone (VAULT-CLONE-001/002)."""
        with self._lock:
            try:
                self._clone()
            except (GitError, OSError):
                raise StartupError("the initial clone of the vault failed") from None

    # @spec VAULT-CLONE-009
    def healthy(self) -> bool:
        """False once fetching has failed for longer than the unhealthy period."""
        since = self._failing_since
        if since is None:
            return True
        return self.clock() - since <= self.settings.unhealthy_after_seconds

    def _wipe(self) -> None:
        workdir = self.settings.workdir
        os.makedirs(workdir, exist_ok=True)
        for name in os.listdir(workdir):
            path = os.path.join(workdir, name)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)

    def _clone(self) -> None:
        """Delete the clone and make a full clone of the branch (raises GitError)."""
        self._wipe()
        token = self.tokens.vault_token("read")
        run(
            self.settings.workdir,
            [
                "clone",
                "--quiet",
                "--no-tags",
                "--single-branch",
                "--branch",
                self.settings.branch,
                "--config",
                "core.precomposeunicode=false",
                "--",
                self.settings.remote_url,
                ".",
            ],
            token=token,
        )

    # the clone ---------------------------------------------------------------
    def _fetch_failed(self) -> VaultError:
        if self._failing_since is None:
            self._failing_since = self.clock()
        return VaultError(
            "fetch_failed",
            {"branch": self.settings.branch, "reason": "the vault's remote could not be fetched"},
        )

    def _fetched(self) -> None:
        self._last_fetch = self.clock()
        self._failing_since = None

    def _corrupt(self) -> bool:
        if not os.path.isdir(os.path.join(self.settings.workdir, ".git")):
            return True
        try:
            self.repo.git("rev-parse", "--verify", "HEAD^{tree}")
            self.repo.git("ls-files", "-z")
            self.repo.git("fsck", "--connectivity-only", "--no-dangling", "--no-progress")
        except GitError:
            return True
        return False

    def _reclone(self) -> None:
        log.warning("the clone is corrupt; cloning again")
        try:
            self._clone()
        except (GitError, OSError):
            raise self._fetch_failed() from None

    def _reset_to_remote(self) -> None:
        self.repo.git("reset", "--hard", "--quiet", f"refs/remotes/origin/{self.settings.branch}")
        self.repo.git("clean", "-ffdxq")

    # @spec VAULT-CLONE-004, VAULT-CLONE-005, VAULT-CLONE-006, VAULT-CLONE-007, VAULT-ID-004
    def _sync(self, *, write: bool) -> None:
        """Fetch the remote branch and reset the clone hard to it, unless a read is fresh."""
        if (
            not write
            and self._last_fetch is not None
            and self.clock() - self._last_fetch < self.settings.freshness_seconds
        ):
            return
        branch = self.settings.branch
        try:
            token = self.tokens.vault_token("read")
            self.repo.git(
                "fetch",
                "--quiet",
                "--no-tags",
                "--prune",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                token=token,
            )
        except GitError:
            if not self._corrupt():
                raise self._fetch_failed() from None
            self._reclone()
        try:
            self._reset_to_remote()
            self.repo.head()
        except GitError:
            self._reclone()
        self._fetched()

    def _snapshot(self) -> Snapshot:
        try:
            snap = Snapshot(self.repo, self.settings)
            snap.load(snap.notes)
        except GitError:
            self._reclone()
            self._fetched()
            snap = Snapshot(self.repo, self.settings)
            snap.load(snap.notes)
        return snap

    def _reset_quietly(self) -> None:
        try:
            self._reset_to_remote()
        except GitError:
            log.warning("could not reset the clone after a failed call")

    # audit -------------------------------------------------------------------
    # @spec VAULT-AUDIT-001, VAULT-AUDIT-002
    def _audit(
        self,
        caller: Identity,
        tool: str,
        paths: list[str],
        count: int,
        commit: str | None,
        outcome: str,
    ) -> None:
        """One line per tool call. Never a body, a field value, a query or a guard pattern."""
        entry = {
            "time": datetime.datetime.now(datetime.UTC).isoformat(),
            "email": caller.email,
            "tool": tool,
            "paths": [_clean(p) for p in paths],
            "count": count,
            "commit": commit,
            "outcome": outcome,
        }
        audit_log.info(json.dumps(entry, ensure_ascii=False))

    def _call(self, caller: Identity, tool: str, body: Callable[[], _Outcome]) -> Result:
        """Run a tool body and write its audit line, success or failure.

        A failed call logs no paths: it read or changed none, and a refused path may itself
        hold guarded text (VAULT-REFUSE-007).
        """
        try:
            result, paths, count, commit = body()
        except VaultError as exc:
            self._audit(caller, tool, [], 0, None, exc.code)
            raise
        except Exception:
            self._audit(caller, tool, [], 0, None, "error")
            raise
        self._audit(caller, tool, paths, count, commit, "ok")
        return result

    # read tools --------------------------------------------------------------
    def _read(self) -> Snapshot:
        self._sync(write=False)
        return self._snapshot()

    # @spec VAULT-SEARCH-001, VAULT-SEARCH-002, VAULT-SEARCH-003, VAULT-SEARCH-004
    # @spec VAULT-SEARCH-005, VAULT-SEARCH-006, VAULT-SEARCH-008, VAULT-CLONE-008
    def search_notes(
        self,
        caller: Identity,
        query: str | None = None,
        folder: str | None = None,
        fields: Fields | None = None,
        limit: int = 25,
        page: int = 1,
    ) -> Result:
        """`{"notes": [{path, name, frontmatter, revision, lines}], "total", "commit"}`."""

        def body() -> _Outcome:
            terms = [t.casefold() for t in (query or "").split()]
            with self._lock:
                snap = self._read()
                scored: list[tuple[int, str, dict[str, Any]]] = []
                for path in self._candidates(snap, folder, fields):
                    text = snap.text(path)
                    haystack = (name_of(path) + "\n" + text).casefold()
                    if not all(t in haystack for t in terms):
                        continue
                    matched = sum(1 for t in set(terms) if t in haystack)
                    entry = self._note_entry(snap, path)
                    entry["lines"] = [
                        _clean(line)
                        for line in text.splitlines()
                        if any(t in line.casefold() for t in terms)
                    ][:3]
                    scored.append((matched, path, entry))
                scored.sort(key=lambda item: (-item[0], item[1]))
                notes = _page([e for _, _, e in scored], limit, page, SEARCH_CAP)
                result = {"notes": notes, "total": len(scored), "commit": snap.commit}
            returned = [n["path"] for n in notes]
            return result, returned, len(returned), None

        return self._call(caller, "search_notes", body)

    # @spec VAULT-TOOL-001, VAULT-NAME-001, VAULT-NAME-002, VAULT-NAME-003, VAULT-NAME-004
    # @spec VAULT-NAME-005, VAULT-FM-004, VAULT-CLONE-008
    def read_note(self, caller: Identity, note: str) -> Result:
        """`{path, frontmatter, frontmatter_unparsed, body, revision, links_to, linked_from,
        commit}`."""

        def body() -> _Outcome:
            with self._lock:
                snap = self._read()
                path = resolve_or_fail(snap.index, note)
                text = snap.text(path)
                fm, unparsed, note_body = frontmatter.read(text)
                links_to = self._targets(snap, path) - {path}
                linked_from = [
                    other
                    for other in snap.resolvable
                    if other != path and path in self._targets(snap, other)
                ]
                result = {
                    "path": _clean(path),
                    "frontmatter": _clean(fm) if isinstance(fm, str) else fm,
                    "frontmatter_unparsed": unparsed,
                    "body": _clean(note_body),
                    "revision": snap.oid(path),
                    "links_to": sorted(_clean(p) for p in links_to),
                    "linked_from": [_clean(p) for p in linked_from],
                    "commit": snap.commit,
                }
            return result, [path], 1, None

        return self._call(caller, "read_note", body)

    # @spec VAULT-SEARCH-002, VAULT-SEARCH-003, VAULT-SEARCH-005, VAULT-SEARCH-007
    # @spec VAULT-SEARCH-008, VAULT-CLONE-008
    def list_notes(
        self,
        caller: Identity,
        folder: str | None = None,
        fields: Fields | None = None,
        limit: int = 100,
        page: int = 1,
    ) -> Result:
        """`{"notes": [{path, name, frontmatter, revision}], "total", "commit"}`."""

        def body() -> _Outcome:
            with self._lock:
                snap = self._read()
                matching = self._candidates(snap, folder, fields)
                times = self.repo.last_change_times()
                ordered = sorted(matching, key=lambda p: (-times.get(snap.repo_path(p), 0), p))
                notes = _page([self._note_entry(snap, p) for p in ordered], limit, page, LIST_CAP)
                result = {"notes": notes, "total": len(ordered), "commit": snap.commit}
            returned = [n["path"] for n in notes]
            return result, returned, len(returned), None

        return self._call(caller, "list_notes", body)

    def _candidates(self, snap: Snapshot, folder: str | None, fields: Fields | None) -> list[str]:
        folder = (folder or "").strip("/")
        if folder and under(folder, snap.trash):
            pool = [p for p in snap.notes if under(p, folder)]
        else:
            pool = [p for p in snap.resolvable if under(p, folder)]
        if not fields:
            return pool
        kept = []
        for path in pool:
            fm, unparsed, _ = frontmatter.read(snap.text(path))
            if unparsed or not isinstance(fm, dict):
                continue
            if all(_field_matches(fm, k, v) for k, v in fields.items()):
                kept.append(path)
        return kept

    def _note_entry(self, snap: Snapshot, path: str) -> dict[str, Any]:
        fm, unparsed, _ = frontmatter.read(snap.text(path))
        return {
            "path": _clean(path),
            "name": _clean(name_of(path)),
            "frontmatter": _clean(fm) if isinstance(fm, str) else fm,
            "frontmatter_unparsed": unparsed,
            "revision": snap.oid(path),
        }

    def _targets(self, snap: Snapshot, path: str) -> set[str]:
        """The resolvable notes `path` links to, unambiguously."""
        found = set()
        for link in links.find_links(snap.text(path)):
            if link.is_self:
                continue
            res = links.resolve(link, path, snap.index)
            if res.target is not None:
                found.add(res.target)
        return found

    # write tools -------------------------------------------------------------
    # Each returns `{path, revision, commit, changed, also_changed: [paths], ...}`.

    # @spec VAULT-TOOL-002, VAULT-NAME-006, VAULT-NAME-007, VAULT-REFUSE-004
    def create_note(
        self, caller: Identity, path: str, body: str, fields: Fields | None = None
    ) -> Result:
        def check() -> None:
            confine(path)
            check_linkable(path)
            self._check_size(path, frontmatter.with_fields(body, fields))

        def plan(snap: Snapshot, lists: refusal_lists.Lists) -> Plan:
            dest = self._destination(snap, path)
            if lists.pinned(dest):
                raise VaultError("refused_pinned", {"path": dest})
            return Plan("create_note", dest, {dest: frontmatter.with_fields(body, fields)})

        return self._change(caller, "create_note", check, plan)

    # @spec VAULT-TOOL-003, VAULT-SECT-001, VAULT-SECT-002, VAULT-SECT-003
    def append_to_note(
        self, caller: Identity, note: str, text: str, under_heading: str | None = None
    ) -> Result:
        def plan(snap: Snapshot, lists: refusal_lists.Lists) -> Plan:
            path = self._source(snap, note)
            parts = frontmatter.split(snap.text(path))
            new_body = sections.append(parts.body, text, under_heading)
            return Plan("append_to_note", path, {path: parts.head + new_body})

        return self._change(caller, "append_to_note", lambda: self._check_arg(note), plan)

    # @spec VAULT-TOOL-004, VAULT-FM-001, VAULT-FM-002, VAULT-FM-003, VAULT-FM-004
    def edit_note(
        self,
        caller: Identity,
        note: str,
        revision: str,
        body: str | None = None,
        section: str | None = None,
        fields: Fields | None = None,
    ) -> Result:
        def plan(snap: Snapshot, lists: refusal_lists.Lists) -> Plan:
            path = self._source(snap, note)
            parts = frontmatter.split(snap.text(path))
            new_body = parts.body
            if body is not None:
                if section is not None:
                    new_body = sections.replace(parts.body, body, section)
                else:
                    new_body = body
            self._check_revision(snap, path, revision)
            new = parts.head + new_body
            if fields:
                new = self._set_fields(path, new, fields)
            return Plan("edit_note", path, {path: new})

        return self._change(caller, "edit_note", lambda: self._check_arg(note), plan)

    # @spec VAULT-TOOL-005, VAULT-TOOL-006, VAULT-NAME-006, VAULT-NAME-007, VAULT-REFUSE-003
    # @spec VAULT-LINK-004, VAULT-LINK-005, VAULT-LINK-006, VAULT-LINK-007, VAULT-LINK-008
    def move_note(self, caller: Identity, note: str, revision: str, new_path: str) -> Result:
        """Also returns `ambiguous_links`: links left alone because they were already ambiguous."""

        def check() -> None:
            self._check_arg(note)
            confine(new_path, what="new_path")
            check_linkable(new_path, what="new_path")
            for folder in (self.settings.trash_folder, self.settings.archive_folder):
                if folder and under(new_path, folder):
                    raise VaultError(
                        "confinement",
                        {
                            "new_path": new_path,
                            "reason": "only delete_note and archive_note move notes there",
                        },
                    )

        def plan(snap: Snapshot, lists: refusal_lists.Lists) -> Plan:
            source: str | None = None
            failure: VaultError | None = None
            try:
                source = self._source(snap, note, include_trash=True)
            except VaultError as exc:
                if exc.code == "confinement":
                    raise
                failure = exc
            dest = self._destination(snap, new_path, moving=source)
            if failure is not None:
                raise failure
            assert source is not None
            self._check_revision(snap, source, revision)
            for p in (source, dest):
                if lists.pinned(p):
                    raise VaultError("refused_pinned", {"note": p})
            if dest == source:
                return Plan("move_note", source, {}, extra={"ambiguous_links": []})
            return self._move_plan(snap, "move_note", source, dest, snap.text(source))

        return self._change(caller, "move_note", check, plan)

    # @spec VAULT-TOOL-007, VAULT-TOOL-008, VAULT-TOOL-010, VAULT-TOOL-011, VAULT-REFUSE-003
    # @spec VAULT-LINK-008, VAULT-LINK-009
    def delete_note(self, caller: Identity, note: str, revision: str) -> Result:
        """Also returns `dangling_links`: how many links now point at the trashed note."""
        trash = self.settings.trash_folder.strip("/")

        def check() -> None:
            self._check_arg(note)
            if looks_like_path(note) and under(note, trash):
                raise VaultError(
                    "confinement", {"note": note, "reason": "the note is already in the trash"}
                )

        def plan(snap: Snapshot, lists: refusal_lists.Lists) -> Plan:
            source = self._source(snap, note)
            self._check_revision(snap, source, revision)
            if lists.pinned(source):
                raise VaultError("refused_pinned", {"note": source})
            dest = self._free_path(snap, f"{trash}/{source}")
            dangling = 0
            for other in snap.resolvable:
                if other == source:
                    continue
                for link in links.find_links(snap.text(other)):
                    if link.is_self:
                        continue
                    res = links.resolve(link, other, snap.index)
                    if res.target == source:
                        dangling += 1
            return Plan(
                "delete_note",
                dest,
                {source: None, dest: snap.text(source)},
                source=source,
                extra={"dangling_links": dangling},
            )

        return self._change(caller, "delete_note", check, plan)

    # @spec VAULT-TOOL-009, VAULT-TOOL-010, VAULT-REFUSE-003
    # @spec VAULT-LINK-004, VAULT-LINK-005, VAULT-LINK-008
    def archive_note(
        self, caller: Identity, note: str, revision: str, fields: Fields | None = None
    ) -> Result:
        archive = (self.settings.archive_folder or "").strip("/")

        def check() -> None:
            if not archive:
                raise VaultError("confinement", {"reason": "no archive folder is configured"})
            self._check_arg(note)

        def plan(snap: Snapshot, lists: refusal_lists.Lists) -> Plan:
            source = self._source(snap, note)
            if under(source, archive):
                raise VaultError(
                    "confinement", {"note": source, "reason": "the note is already archived"}
                )
            self._check_revision(snap, source, revision)
            text = snap.text(source)
            if fields:
                text = self._set_fields(source, text, fields)
            if lists.pinned(source):
                raise VaultError("refused_pinned", {"note": source})
            dest = self._free_path(snap, f"{archive}/{source}")
            return self._move_plan(snap, "archive_note", source, dest, text)

        return self._change(caller, "archive_note", check, plan)

    # write helpers -----------------------------------------------------------
    def _check_arg(self, note: str) -> None:
        """A note argument naming a path must be a confined one (VAULT-WRITE-007)."""
        if "/" in note or "\\" in note or (is_note_path(note) and note.startswith(".")):
            confine(note, what="note")

    # @spec VAULT-WRITE-008, VAULT-WRITE-009
    def _check_size(self, path: str, text: str) -> None:
        size = len(_bytes(text))
        if size > self.settings.size_cap_bytes:
            raise VaultError(
                "too_large", {"path": path, "size": size, "cap": self.settings.size_cap_bytes}
            )

    # @spec VAULT-WRITE-003, VAULT-ERR-003
    def _check_revision(self, snap: Snapshot, path: str, revision: str) -> None:
        current = snap.oid(path)
        if revision != current:
            raise VaultError(
                "stale_revision", {"note": path, "given": revision, "current_revision": current}
            )

    def _set_fields(self, path: str, text: str, fields: Fields) -> str:
        try:
            return frontmatter.set_fields(text, fields)
        except frontmatter.FrontmatterUnparsed:
            raise VaultError(
                "frontmatter_unparsed",
                {"note": path, "reason": "the note's frontmatter does not parse as YAML"},
            ) from None

    def _source(self, snap: Snapshot, note: str, *, include_trash: bool = False) -> str:
        """The existing note a changing call targets."""
        if looks_like_path(note):
            if snap.symlink_on(note) is not None:
                raise VaultError(
                    "confinement", {"note": note, "reason": "passes through a symbolic link"}
                )
            if include_trash:
                for p in snap.notes:
                    if key(p) in (key(note), key(note + ".md")):
                        return p
        return resolve_or_fail(snap.index, note)

    # @spec VAULT-NAME-006, VAULT-WRITE-007
    def _destination(self, snap: Snapshot, path: str, *, moving: str | None = None) -> str:
        """Where a created or moved note will live, spelled with existing folders' case."""
        if snap.symlink_on(path) is not None:
            raise VaultError(
                "confinement", {"path": path, "reason": "passes through a symbolic link"}
            )
        parts = path.split("/")
        for i in range(1, len(parts)):
            existing = snap.by_key.get(key("/".join(parts[:i])))
            if existing is not None:
                raise VaultError(
                    "confinement", {"path": path, "reason": f"{existing} is a file, not a folder"}
                )
        folder = _respell(snap, "/".join(parts[:-1]))
        dest = f"{folder}/{parts[-1]}" if folder else parts[-1]
        clash = snap.by_key.get(key(dest))
        if clash is not None and clash != moving:
            raise VaultError(
                "confinement",
                {"path": path, "existing": clash, "reason": "a note exists at an equivalent path"},
            )
        return dest

    def _free_path(self, snap: Snapshot, path: str) -> str:
        """`path`, or the first of `name 1.md`, `name 2.md`... that is free (VAULT-TOOL-010)."""
        folder, base = posixpath.split(path)
        folder = _respell(snap, folder)
        stem, ext = base[:-3], base[-3:]
        candidate = f"{folder}/{base}" if folder else base
        n = 0
        while key(candidate) in snap.by_key:
            n += 1
            name = f"{stem} {n}{ext}"
            candidate = f"{folder}/{name}" if folder else name
        return candidate

    def _move_plan(self, snap: Snapshot, tool: str, source: str, dest: str, text: str) -> Plan:
        """Move `source` to `dest` holding `text`, rewriting links so they keep working."""
        new_paths = [p for p in snap.resolvable if p != source]
        if not is_hidden(dest) and not under(dest, snap.trash):
            new_paths.append(dest)
        new_index = Index(new_paths)
        files: dict[str, str | None] = {source: None}
        also: list[str] = []
        ambiguous: list[dict[str, str]] = []
        notes = list(snap.resolvable)
        if source not in snap.resolvable:
            notes.append(source)
        for note in notes:
            old_text = text if note == source else snap.text(note)
            here = dest if note == source else note
            edits: list[tuple[int, int, str]] = []
            for link in links.find_links(old_text):
                if link.is_self:
                    continue
                before = links.resolve(link, note, snap.index)
                if before.ambiguous:
                    if source in before.candidates:
                        written = old_text[link.start : link.end]
                        ambiguous.append({"path": _clean(here), "link": _clean(written)})
                    continue
                if before.target is None:
                    continue
                expected = dest if before.target == source else before.target
                if new_index.path(expected) != expected:
                    continue
                after = links.resolve(link, here, new_index)
                if after.target == expected:
                    continue
                rewritten = links.rewrite_target(link, expected, here, new_index)
                edits.append((link.start, link.end, rewritten))
            new_text = old_text
            for start, end, replacement in sorted(edits, reverse=True):
                new_text = new_text[:start] + replacement + new_text[end:]
            if note == source:
                files[dest] = new_text
            elif new_text != old_text:
                files[note] = new_text
                also.append(note)
        return Plan(
            tool,
            dest,
            files,
            source=source,
            also_changed=also,
            extra={"ambiguous_links": ambiguous},
        )

    def _message(self, plan: Plan) -> str:
        """`vault: <verb> <paths>`, then the other notes changed (VAULT-WRITE-001)."""
        verb = _VERBS[plan.tool]
        pre = self._prefix()
        if plan.source is not None and plan.source != plan.target:
            head = f"vault: {verb} {pre}{plan.source} -> {pre}{plan.target}"
        else:
            head = f"vault: {verb} {pre}{plan.target}"
        lines = [head, ""]
        if plan.also_changed:
            lines.append("Links rewritten in:")
            lines += [f"- {pre}{p}" for p in plan.also_changed]
            lines.append("")
        return "\n".join(lines)

    def _prefix(self) -> str:
        sub = self.settings.subdirectory.strip("/")
        return sub + "/" if sub else ""

    # @spec VAULT-REFUSE-006, VAULT-REFUSE-007, VAULT-REFUSE-008
    def _guard(self, snap: Snapshot, plan: Plan, lists: refusal_lists.Lists, message: str) -> None:
        """Refuse a call that would raise the guard match count; report only the count."""
        if not lists.guards:
            return
        before = after = 0
        for path, new in plan.files.items():
            repo_path = snap.repo_path(path)
            if path in snap.entries:
                before += lists.matches(snap.text(path)) + lists.matches(repo_path)
            if new is not None:
                after += lists.matches(new) + lists.matches(repo_path)
        # the message names every changed path, and the paths are already counted above
        named = sum(lists.matches(snap.repo_path(p)) for p in plan.files)
        after += max(0, lists.matches(message) - named)
        if after > before:
            raise VaultError("refused_guard", {"added_matches": after - before})

    def _finish_checks(
        self, snap: Snapshot, plan: Plan, lists: refusal_lists.Lists, message: str
    ) -> None:
        """The checks every changing call shares, after its own (VAULT-ERR-002 order)."""
        self._guard(snap, plan, lists, message)
        # @spec VAULT-REFUSE-010
        if lists.unavailable:
            raise VaultError(
                "refusal_source_unavailable",
                {"reason": "a configured refusal source cannot be read now"},
            )
        for path, new in plan.files.items():
            if new is not None:
                self._check_size(path, new)

    # @spec VAULT-REFUSE-005
    def _warn_unmatched_pins(self, snap: Snapshot, lists: refusal_lists.Lists) -> None:
        notes = {key(p) for p in snap.notes}
        for pin in lists.pins:
            if key(pin.strip("/")) not in notes:
                log.warning("pinned path names no note: %s", pin)

    def _changed(self, snap: Snapshot, plan: Plan) -> bool:
        for path, new in plan.files.items():
            if new is None:
                if path in snap.entries:
                    return True
            elif path not in snap.entries or snap.text(path) != new:
                return True
        return False

    def _commit(
        self, snap: Snapshot, plan: Plan, caller: Identity, message: str
    ) -> tuple[str, dict[str, str]]:
        changes: dict[str, tuple[str, str] | None] = {}
        oids: dict[str, str] = {}
        moved_mode = "100644"
        if plan.source is not None and plan.source in snap.entries:
            moved_mode = snap.entries[plan.source].mode
        for path, new in plan.files.items():
            repo_path = snap.repo_path(path)
            if new is None:
                changes[repo_path] = None
                continue
            oid = self.repo.hash_blob(_bytes(new))
            oids[path] = oid
            mode = snap.entries[path].mode if path in snap.entries else moved_mode
            changes[repo_path] = (mode, oid)
        tree = self.repo.write_tree(snap.commit, changes)
        commit = self.repo.commit_tree(
            tree,
            snap.commit,
            f"{message}\n{CALLER_TRAILER}: {caller.email}\n",
            author=(caller.author_name, caller.author_email),
            committer=COMMITTER,
        )
        return commit, oids

    def _push(self, commit: str) -> bool:
        """Push `commit` to the branch. False when the branch moved (VAULT-WRITE-004)."""
        branch = self.settings.branch
        token = self.tokens.vault_token("write")
        try:
            self.repo.git(
                "push", "--quiet", "origin", f"{commit}:refs/heads/{branch}", token=token
            )
        except GitError as exc:
            if any(marker in exc.output for marker in _PUSH_REJECTED):
                return False
            raise VaultError(
                "push_conflict", {"reason": "the push failed", "branch": branch}
            ) from None
        self.repo.git("update-ref", f"refs/remotes/origin/{branch}", commit)
        self._reset_to_remote()
        return True

    # @spec VAULT-CLONE-003, VAULT-ID-003, VAULT-ID-005, VAULT-REFUSE-001, VAULT-REFUSE-009
    # @spec VAULT-WRITE-001, VAULT-WRITE-002, VAULT-WRITE-004, VAULT-WRITE-005, VAULT-WRITE-006
    # @spec VAULT-TOOL-012, VAULT-ERR-001, VAULT-ERR-002, VAULT-ERR-004
    def _change(
        self,
        caller: Identity,
        tool: str,
        check: Callable[[], None],
        plan_for: Callable[[Snapshot, refusal_lists.Lists], Plan],
    ) -> Result:
        """A changing call: static checks, then up to two fetch-plan-commit-push attempts."""

        def body() -> _Outcome:
            check()
            with self._lock:
                try:
                    for _attempt in range(2):
                        self._sync(write=True)
                        snap = self._snapshot()
                        lists = refusal_lists.read(self.settings, self.refusals)
                        self._warn_unmatched_pins(snap, lists)
                        plan = plan_for(snap, lists)
                        message = self._message(plan)
                        self._finish_checks(snap, plan, lists, message)
                        if not self._changed(snap, plan):
                            unchanged: Result = {
                                "path": _clean(plan.target),
                                "revision": snap.oid(plan.target),
                                "changed": False,
                                "answered_from": snap.commit,
                                "also_changed": [],
                                "note": "nothing would change, so no commit was made",
                                **plan.extra,
                            }
                            return unchanged, [], 0, None
                        commit, oids = self._commit(snap, plan, caller, message)
                        if self._push(commit):
                            paths = list(plan.files)
                            result: Result = {
                                "path": _clean(plan.target),
                                "revision": oids[plan.target],
                                "commit": commit,
                                "changed": True,
                                "also_changed": [_clean(p) for p in plan.also_changed],
                                **plan.extra,
                            }
                            return result, paths, len(paths), commit
                    raise VaultError(
                        "push_conflict",
                        {
                            "reason": "the branch moved during both attempts",
                            "branch": self.settings.branch,
                        },
                    )
                except BaseException:
                    self._reset_quietly()
                    raise

        return self._call(caller, tool, body)


def _respell(snap: Snapshot, folder: str) -> str:
    """`folder` with each component spelled as an existing equivalent folder spells it."""
    if not folder:
        return folder
    parts = folder.split("/")
    out: list[str] = []
    for part in parts:
        existing = snap.dirs.get(key("/".join([*out, part])))
        out.append(existing.split("/")[-1] if existing else part)
    return "/".join(out)


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _field_matches(fm: Mapping[str, Any], name: str, wanted: Any) -> bool:
    if name not in fm:
        return False
    value = fm[name]
    if isinstance(value, list):
        return any(_as_text(item) == _as_text(wanted) for item in value)
    return _as_text(value) == _as_text(wanted)


def _page(items: list[dict[str, Any]], limit: int, page: int, cap: int) -> list[dict[str, Any]]:
    size = max(1, min(int(limit), cap))
    start = (max(1, int(page)) - 1) * size
    return items[start : start + size]
