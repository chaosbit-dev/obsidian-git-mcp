"""GitHub App installation tokens for the vault repository (VAULT-ID-004).

Every call mints a fresh installation token, narrowed to the vault repository and to the
permission the operation needs. Nothing is cached: a token lives in memory for the one
fetch or push that asked for it, and is never written to disk or to a log line (VAULT-ID-006).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import httpx2
import jwt

if TYPE_CHECKING:
    from collections.abc import Callable

Permission = Literal["read", "write"]

DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
# GitHub rejects an App JWT whose lifetime exceeds ten minutes, and one issued "in the future"
# by a clock that runs ahead of GitHub's; backdating `iat` absorbs that skew.
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 540


class TokenError(OSError):
    """A token could not be minted. The message never contains a credential."""


def repository_name(remote_url: str) -> str:
    """The bare repository name in a GitHub remote URL: `.../owner/repo(.git)` -> `repo`.

    Accepts `https://github.com/owner/repo.git` and `git@github.com:owner/repo.git`.
    """
    path = urlsplit(remote_url).path if "://" in remote_url else remote_url.rpartition(":")[2]
    name = path.rstrip("/").rpartition("/")[2]
    name = name.removesuffix(".git")
    if not name:
        raise ValueError(f"cannot find a repository name in {remote_url!r}")
    return name


class GitHubApp:
    """One App installation, able to mint narrowed installation tokens."""

    def __init__(
        self,
        *,
        app_id: str,
        installation_id: int,
        private_key: str,
        api_url: str = DEFAULT_API_URL,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        timeout: float = 15.0,
    ) -> None:
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = private_key
        self._clock = clock
        self._http = httpx2.Client(
            base_url=api_url.rstrip("/"),
            transport=transport,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "obsidian-git-mcp",
            },
        )

    def _app_jwt(self) -> str:
        now = int(self._clock())
        claims = {
            "iat": now - JWT_BACKDATE_SECONDS,
            "exp": now + JWT_LIFETIME_SECONDS,
            "iss": self._app_id,
        }
        try:
            return jwt.encode(claims, self._private_key, algorithm="RS256")
        except (ValueError, TypeError, jwt.PyJWTError) as error:
            # The key's own text never reaches the message.
            raise TokenError(f"the App private key is unusable ({type(error).__name__})") from None

    def installation_token(self, repository: str, permission: Permission) -> str:
        """A token for `repository` alone, with `contents: <permission>` and nothing else."""
        url = f"/app/installations/{self._installation_id}/access_tokens"
        body = {"repositories": [repository], "permissions": {"contents": permission}}
        try:
            response = self._http.post(
                url, json=body, headers={"Authorization": f"Bearer {self._app_jwt()}"}
            )
        except httpx2.HTTPError as error:
            raise TokenError(f"minting a token failed: {type(error).__name__}") from None
        if response.status_code != 201:
            raise TokenError(f"minting a token failed: HTTP {response.status_code}")
        try:
            token = response.json()["token"]
        except (ValueError, KeyError, TypeError):
            raise TokenError("minting a token failed: the response carried no token") from None
        if not isinstance(token, str) or not token:
            raise TokenError("minting a token failed: the response carried no token")
        return token

    def close(self) -> None:
        self._http.close()


class VaultTokens:
    """The service's `TokenSource`: one fresh token per git operation on the vault."""

    def __init__(self, app: GitHubApp, repository: str) -> None:
        self._app = app
        self._repository = repository

    # @spec VAULT-ID-004
    def vault_token(self, permission: Permission) -> str:
        if permission not in ("read", "write"):
            raise ValueError(f"unknown permission {permission!r}")
        return self._app.installation_token(self._repository, permission)
