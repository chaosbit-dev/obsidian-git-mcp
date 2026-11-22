"""Reading refusal lists from another GitHub repository (VAULT-ID-005).

A refusal source's `location` names a file in a GitHub repository:

    owner/repo:path/to/file@ref

`@ref` is optional (a branch, tag or commit; without it GitHub serves the repository's
default branch). The ref is split at the last `@`, so a path may itself contain `@` as
long as a ref is then given. Examples:

    chaosbit-dev/holocron:superseded.yaml@main
    chaosbit-dev/holocron:config/guards.yaml

The file is fetched through the contents API with the credential configured for refusal
sources, which is never a token minted for the vault: the reader has no access to the
vault's `TokenSource` at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx2

from obsidian_git_mcp.github_app import API_VERSION, DEFAULT_API_URL

if TYPE_CHECKING:
    from collections.abc import Callable

    from obsidian_git_mcp.config import RefusalSource

# GitHub owner and repository names: letters, digits, '-', '_' and '.'.
_NAME = r"[A-Za-z0-9_.-]+"
_LOCATION = re.compile(rf"^(?P<owner>{_NAME})/(?P<repo>{_NAME}):(?P<rest>.+)$")

# A refusal list is a few kilobytes; anything far larger is not one.
MAX_BYTES = 1_000_000


class RefusalSourceError(OSError):
    """A refusal source could not be read. The message never contains a credential."""


@dataclass(frozen=True)
class Location:
    owner: str
    repo: str
    path: str
    ref: str | None

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_location(location: str) -> Location:
    """Parse `owner/repo:path[@ref]`; raises ValueError when it does not match."""
    match = _LOCATION.match(location.strip())
    if match is None:
        raise ValueError(f"refusal source {location!r} is not 'owner/repo:path[@ref]'")
    rest = match["rest"]
    path, at, ref = rest.rpartition("@")
    if not at:
        path, ref = rest, ""
    path = path.strip("/")
    if not path or (at and not ref):
        raise ValueError(f"refusal source {location!r} is not 'owner/repo:path[@ref]'")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError(f"refusal source {location!r} has an invalid path")
    return Location(match["owner"], match["repo"], path, ref or None)


class GitHubRefusalReader:
    """The service's `RefusalReader`.

    `credential` is called with `owner/repo` for each read and returns the token to use: a
    fixed fine-grained token, or one minted by a second, read-only App. It is configured
    separately from the vault's App and must never be the vault's token source.
    """

    def __init__(
        self,
        credential: Callable[[str], str],
        *,
        api_url: str = DEFAULT_API_URL,
        transport: httpx2.BaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._credential = credential
        self._http = httpx2.Client(
            base_url=api_url.rstrip("/"),
            transport=transport,
            timeout=timeout,
            headers={
                # The raw media type returns the file's bytes rather than base64 in JSON.
                "Accept": "application/vnd.github.raw+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "obsidian-git-mcp",
            },
        )

    # @spec VAULT-ID-005
    def read(self, source: RefusalSource) -> str:
        try:
            where = parse_location(source.location)
        except ValueError as error:
            raise RefusalSourceError(str(error)) from None
        token = self._credential(where.repository)
        url = f"/repos/{where.owner}/{where.repo}/contents/{quote(where.path)}"
        params = {"ref": where.ref} if where.ref else None
        try:
            response = self._http.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx2.HTTPError as error:
            raise RefusalSourceError(
                f"reading {source.location} failed: {type(error).__name__}"
            ) from None
        if response.status_code != 200:
            raise RefusalSourceError(
                f"reading {source.location} failed: HTTP {response.status_code}"
            )
        if len(response.content) > MAX_BYTES:
            raise RefusalSourceError(f"reading {source.location} failed: larger than {MAX_BYTES}")
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError:
            raise RefusalSourceError(f"reading {source.location} failed: not UTF-8") from None

    def close(self) -> None:
        self._http.close()


def static_credential(token: str) -> Callable[[str], str]:
    """A fixed token (a fine-grained token with `contents: read` on the source repository)."""

    def credential(_repository: str) -> str:
        return token

    return credential
