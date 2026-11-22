"""Failure codes, in the order a call reports them when several apply (VAULT-ERR-002)."""

from __future__ import annotations

from typing import Any

CODES: tuple[str, ...] = (
    "confinement",
    "not_found",
    "ambiguous",
    "stale_revision",
    "frontmatter_unparsed",
    "refused_pinned",
    "refused_guard",
    "refusal_source_unavailable",
    "too_large",
    "fetch_failed",
    "push_conflict",
)


class VaultError(Exception):
    """A tool call failed. `code` is one of CODES; `detail` says what it concerns."""

    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        if code not in CODES:
            raise ValueError(f"unknown code {code!r}")
        self.code = code
        self.detail = detail or {}
        super().__init__(code)


class StartupError(Exception):
    """The server cannot start serving."""
