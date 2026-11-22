"""The vault: one git clone, serialised tool calls, and the write rules."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obsidian_git_mcp.config import Identity, Settings
    from obsidian_git_mcp.ports import RefusalReader, TokenSource

AUDIT_LOGGER = "obsidian_git_mcp.audit"

Fields = Mapping[str, Any]
Result = dict[str, Any]


class VaultService:
    """Every tool is a method taking the caller's identity first.

    Every result dict carries `commit`: the commit it was answered from (read) or
    made (write; absent when a write changed nothing, with `changed: False`).
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

    # lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        """Wipe the workdir and clone (VAULT-CLONE-001/002)."""
        raise NotImplementedError

    def healthy(self) -> bool:
        """False once fetching has failed for longer than the unhealthy period."""
        raise NotImplementedError

    # read tools --------------------------------------------------------------
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
        raise NotImplementedError

    def read_note(self, caller: Identity, note: str) -> Result:
        """`{path, frontmatter, frontmatter_unparsed, body, revision, links_to, linked_from,
        commit}`."""
        raise NotImplementedError

    def list_notes(
        self,
        caller: Identity,
        folder: str | None = None,
        fields: Fields | None = None,
        limit: int = 100,
        page: int = 1,
    ) -> Result:
        """`{"notes": [{path, name, frontmatter, revision}], "total", "commit"}`."""
        raise NotImplementedError

    # write tools -------------------------------------------------------------
    # Each returns `{path, revision, commit, changed, also_changed: [paths], ...}`.
    def create_note(
        self, caller: Identity, path: str, body: str, fields: Fields | None = None
    ) -> Result:
        raise NotImplementedError

    def append_to_note(
        self, caller: Identity, note: str, text: str, under_heading: str | None = None
    ) -> Result:
        raise NotImplementedError

    def edit_note(
        self,
        caller: Identity,
        note: str,
        revision: str,
        body: str | None = None,
        section: str | None = None,
        fields: Fields | None = None,
    ) -> Result:
        raise NotImplementedError

    def move_note(self, caller: Identity, note: str, revision: str, new_path: str) -> Result:
        """Also returns `ambiguous_links`: links left alone because they were already ambiguous."""
        raise NotImplementedError

    def delete_note(self, caller: Identity, note: str, revision: str) -> Result:
        """Also returns `dangling_links`: how many links now point at the trashed note."""
        raise NotImplementedError

    def archive_note(
        self, caller: Identity, note: str, revision: str, fields: Fields | None = None
    ) -> Result:
        raise NotImplementedError
