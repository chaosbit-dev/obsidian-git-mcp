"""What the service needs from outside: tokens for the vault, and refusal-list text."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from obsidian_git_mcp.config import RefusalSource


class TokenSource(Protocol):
    def vault_token(self, permission: Literal["read", "write"]) -> str:
        """A GitHub App installation token for the vault repository (VAULT-ID-004)."""
        ...


class RefusalReader(Protocol):
    def read(self, source: RefusalSource) -> str:
        """The text of a refusal source, read with the refusal-source credential (VAULT-ID-005).

        Raises OSError when the source cannot be read.
        """
        ...
