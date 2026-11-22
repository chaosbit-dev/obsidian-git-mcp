"""The GitHub App token source, against a mocked GitHub API (VAULT-ID-004, VAULT-ID-006)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from obsidian_git_mcp.github_app import GitHubApp, TokenError, VaultTokens, repository_name

if TYPE_CHECKING:
    from collections.abc import Callable

APP_ID = "424242"
INSTALLATION = 99
NOW = 1_800_000_000.0


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


class GitHub:
    """Records every request and answers with a fresh token each time."""

    def __init__(self, respond: Callable[[httpx2.Request], httpx2.Response] | None = None):
        self.requests: list[httpx2.Request] = []
        self._respond = respond

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self._respond is not None:
            return self._respond(request)
        return httpx2.Response(201, json={"token": f"ghs_minted{len(self.requests)}"})

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)


def make_tokens(pem: str, github: GitHub, repository: str = "datapad") -> VaultTokens:
    app = GitHubApp(
        app_id=APP_ID,
        installation_id=INSTALLATION,
        private_key=pem,
        transport=httpx2.MockTransport(github),
        clock=lambda: NOW,
    )
    return VaultTokens(app, repository)


# @spec VAULT-ID-004
@pytest.mark.parametrize("permission", ["read", "write"])
def test_token_is_narrowed_to_the_vault_repository_and_the_permission(
    pem: str, permission: str
) -> None:
    github = GitHub()
    token = make_tokens(pem, github).vault_token(permission)  # type: ignore[arg-type]
    assert token == "ghs_minted1"
    request = github.requests[0]
    assert request.method == "POST"
    assert request.url.path == f"/app/installations/{INSTALLATION}/access_tokens"
    assert github.body() == {
        "repositories": ["datapad"],
        "permissions": {"contents": permission},
    }


# @spec VAULT-ID-004
def test_app_jwt_is_signed_with_the_app_key_and_short_lived(
    pem: str, key: rsa.RSAPrivateKey
) -> None:
    github = GitHub()
    make_tokens(pem, github).vault_token("read")
    scheme, _, token = github.requests[0].headers["Authorization"].partition(" ")
    assert scheme == "Bearer"
    claims = jwt.decode(
        token,
        key.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == APP_ID
    assert claims["iat"] <= NOW
    assert claims["exp"] - claims["iat"] <= 600  # GitHub's ten-minute ceiling


# @spec VAULT-ID-004
def test_every_operation_mints_its_own_token(pem: str) -> None:
    github = GitHub()
    tokens = make_tokens(pem, github)
    first = tokens.vault_token("read")
    second = tokens.vault_token("write")
    assert first != second
    assert len(github.requests) == 2
    assert [github.body(i)["permissions"]["contents"] for i in (0, 1)] == ["read", "write"]


# @spec VAULT-ID-004
def test_an_unknown_permission_is_refused_before_any_request(pem: str) -> None:
    github = GitHub()
    with pytest.raises(ValueError):
        make_tokens(pem, github).vault_token("admin")  # type: ignore[arg-type]
    assert github.requests == []


# @spec VAULT-ID-004, VAULT-ID-006
@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(401, json={"message": "Bad credentials"}),
        httpx2.Response(422, json={"message": "repository not in installation"}),
        httpx2.Response(201, json={"expires_at": "soon"}),
        httpx2.Response(201, content=b"not json"),
    ],
)
def test_a_failed_mint_raises_an_oserror_without_credentials(
    pem: str, response: httpx2.Response
) -> None:
    github = GitHub(lambda _request: response)
    with pytest.raises(OSError) as raised:
        make_tokens(pem, github).vault_token("write")
    assert isinstance(raised.value, TokenError)
    sent_jwt = github.requests[0].headers["Authorization"].partition(" ")[2]
    assert sent_jwt not in str(raised.value)
    assert "PRIVATE KEY" not in str(raised.value)


# @spec VAULT-ID-004, VAULT-ID-006
def test_a_network_failure_raises_an_oserror(pem: str) -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down", request=request)

    with pytest.raises(TokenError):
        make_tokens(pem, GitHub(fail)).vault_token("read")


# @spec VAULT-ID-006
def test_an_unusable_key_does_not_echo_the_key() -> None:
    bad_key = "-----BEGIN PRIVATE KEY-----\nnot-a-key-secretmaterial\n-----END PRIVATE KEY-----"
    github = GitHub()
    with pytest.raises(TokenError) as raised:
        make_tokens(bad_key, github).vault_token("read")
    assert "secretmaterial" not in str(raised.value)
    assert github.requests == []


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://github.com/chaosbit-dev/datapad.git", "datapad"),
        ("https://github.com/chaosbit-dev/datapad", "datapad"),
        ("https://github.com/chaosbit-dev/datapad/", "datapad"),
        ("git@github.com:chaosbit-dev/datapad.git", "datapad"),
    ],
)
def test_repository_name_from_remote_url(url: str, name: str) -> None:
    assert repository_name(url) == name
