"""Configuration: everything estate-specific reaches the server through here."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["read", "write"]
Extraction = Literal["list", "yaml-mapping"]


@dataclass(frozen=True)
class Identity:
    """One identity-map entry: who may call, in which mode, and how their commits are authored."""

    email: str
    mode: Mode
    author_name: str
    author_email: str


@dataclass(frozen=True)
class RefusalSource:
    """Where a refusal list is read from, and how entries are extracted from it.

    `location` is opaque to the service; a `RefusalReader` turns it into text.
    For `yaml-mapping`, entries are the mapping's keys and values that start with
    `prefix`, with the prefix removed. For `list`, each non-empty line is an entry.
    """

    location: str
    extraction: Extraction = "list"
    prefix: str = ""


@dataclass(frozen=True)
class Settings:
    remote_url: str
    branch: str
    workdir: str
    identities: tuple[Identity, ...]
    allowed_hosts: tuple[str, ...]
    access_team_domain: str
    access_audience: str
    subdirectory: str = ""
    freshness_seconds: float = 15.0
    unhealthy_after_seconds: float = 600.0
    trash_folder: str = "trash"
    archive_folder: str | None = None
    size_cap_bytes: int = 1_000_000
    pinned_sources: tuple[RefusalSource, ...] = field(default_factory=tuple)
    guard_source: RefusalSource | None = None


def validate(settings: Settings) -> None:
    """Raise StartupError if the settings cannot be served (VAULT-ID-001, VAULT-ACC-008)."""
    raise NotImplementedError
