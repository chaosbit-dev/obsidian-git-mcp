"""Perimeter and identity tests: Access JWT, host check, health, startup validation, tool sets.

Requests go through the real ASGI app (server.build_app) over an httpx2 ASGI transport, with the
Access signing keys served by the AccessKeys fixture. A refusal is checked to have happened
before the MCP application: the request is a well-formed MCP `initialize`, so a response that
carries an MCP session id or a JSON-RPC body would mean the MCP application answered it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import anyio
import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import LATEST_PROTOCOL_VERSION

from obsidian_git_mcp import config, server
from obsidian_git_mcp.config import Identity, Settings
from obsidian_git_mcp.errors import StartupError
from obsidian_git_mcp.server import ARCHIVE_TOOL, READ_TOOLS, WRITE_TOOLS
from obsidian_git_mcp.service import VaultService
from tests.conftest import (
    AUDIENCE,
    HOST,
    OWNER,
    OWNER_EMAIL,
    READER,
    READER_EMAIL,
    TEAM_DOMAIN,
    AccessKeys,
    Clock,
    FakeRefusals,
    FakeTokens,
    Remote,
    make_settings,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.types import ASGIApp, Message

    from obsidian_git_mcp.config import Mode

JWT_HEADER = "Cf-Access-Jwt-Assertion"
SESSION_HEADER = "mcp-session-id"
BASE_URL = f"http://{HOST}"
DEADLINE = 20.0

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "test_access", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


# --- helpers ------------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: ASGIApp) -> AsyncIterator[None]:
    """Run the app's ASGI lifespan, as uvicorn would, around the block."""
    to_app: asyncio.Queue[Message] = asyncio.Queue()
    from_app: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return await to_app.get()

    async def send(message: Message) -> None:
        await from_app.put(message)

    scope: dict[str, Any] = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}

    async def run() -> None:
        await app(scope, receive, send)

    task = asyncio.create_task(run())

    async def expect(kind: str) -> None:
        getter = asyncio.create_task(from_app.get())
        done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
        if getter not in done:
            getter.cancel()
            task.result()  # re-raises the app's failure
            raise AssertionError(f"lifespan ended without {kind}")
        message = getter.result()
        assert message["type"] == kind, message

    await to_app.put({"type": "lifespan.startup"})
    await expect("lifespan.startup.complete")
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        await expect("lifespan.shutdown.complete")
        await task


@asynccontextmanager
async def http(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> AsyncIterator[httpx2.AsyncClient]:
    """An HTTP client talking to the real app, with the app's lifespan running."""
    app = server.build_app(settings, service, access_transport=access_keys.transport())
    async with lifespan(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            yield client


async def post_initialize(
    client: httpx2.AsyncClient, token: str | None, *, host: str | None = None
) -> httpx2.Response:
    headers = dict(MCP_HEADERS)
    if token is not None:
        headers[JWT_HEADER] = token
    if host is not None:
        headers["Host"] = host
    with anyio.fail_after(DEADLINE):
        return await client.post("/mcp", json=INITIALIZE, headers=headers)


def assert_refused_before_mcp(response: httpx2.Response, status: int) -> None:
    assert response.status_code == status
    assert SESSION_HEADER not in response.headers
    assert '"jsonrpc"' not in response.text


async def tool_names(
    settings: Settings, service: VaultService, access_keys: AccessKeys, email: str
) -> set[str]:
    """Initialise an MCP session as `email` through the real client and list its tools."""
    app = server.build_app(settings, service, access_transport=access_keys.transport())
    async with lifespan(app):
        transport = httpx2.ASGITransport(app=app)
        headers = {JWT_HEADER: access_keys.token(email)}
        async with httpx2.AsyncClient(
            transport=transport, base_url=BASE_URL, headers=headers
        ) as client:
            with anyio.fail_after(DEADLINE):
                async with (
                    streamable_http_client(f"{BASE_URL}/mcp", http_client=client) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    listed = await session.list_tools()
    return {tool.name for tool in listed.tools}


def b64url(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def valid_claims(email: str = OWNER_EMAIL) -> dict[str, Any]:
    now = int(time.time())
    return {
        "aud": [AUDIENCE],
        "iss": f"https://{TEAM_DOMAIN}",
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "email": email,
    }


def started_service(
    settings: Settings, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> VaultService:
    svc = VaultService(settings, tokens, refusals, clock=clock)
    svc.start()
    return svc


# --- VAULT-ACC-001: the endpoint ----------------------------------------------------------


# @spec VAULT-ACC-001
async def test_mcp_endpoint_completes_an_mcp_session_at_slash_mcp(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    names = await tool_names(settings, service, access_keys, OWNER_EMAIL)
    assert "read_note" in names


# @spec VAULT-ACC-001
async def test_authorised_initialize_post_to_slash_mcp_is_answered_by_mcp(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    async with http(settings, service, access_keys) as client:
        response = await post_initialize(client, access_keys.token(OWNER_EMAIL))
    assert response.status_code == 200
    assert '"jsonrpc"' in response.text


# @spec VAULT-ACC-001
async def test_bare_hostname_is_not_an_mcp_endpoint(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    async with http(settings, service, access_keys) as client:
        response = await client.post(
            "/",
            json=INITIALIZE,
            headers={**MCP_HEADERS, JWT_HEADER: access_keys.token(OWNER_EMAIL)},
        )
    assert response.status_code == 404
    assert '"jsonrpc"' not in response.text


# --- VAULT-ACC-002: no JWT ----------------------------------------------------------------


# @spec VAULT-ACC-002
async def test_request_without_access_jwt_is_refused_401(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    async with http(settings, service, access_keys) as client:
        response = await post_initialize(client, None)
    assert_refused_before_mcp(response, 401)


# @spec VAULT-ACC-002
async def test_empty_access_jwt_header_is_refused_401(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    async with http(settings, service, access_keys) as client:
        response = await post_initialize(client, "")
    assert_refused_before_mcp(response, 401)


# @spec VAULT-ACC-002
async def test_jwt_in_authorization_header_instead_of_access_header_is_refused_401(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    """Only Cloudflare's header counts: a bearer token is not an Access assertion."""
    async with http(settings, service, access_keys) as client:
        response = await client.post(
            "/mcp",
            json=INITIALIZE,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {access_keys.token()}"},
        )
    assert_refused_before_mcp(response, 401)


# --- VAULT-ACC-003: JWT validation --------------------------------------------------------


def _token_cases(keys: AccessKeys) -> dict[str, str]:
    now = int(time.time())
    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hs256 = jwt.encode(
        valid_claims(), "shared-secret", algorithm="HS256", headers={"kid": keys.current}
    )
    unsigned = b64url({"alg": "none", "typ": "JWT", "kid": keys.current}) + "."
    unsigned += b64url(valid_claims()) + "."
    return {
        "forged_signature": keys.token(key=foreign),
        "hs256": hs256,
        "alg_none": unsigned,
        "garbage": "not-a-jwt",
        "wrong_audience": keys.token(aud=["some-other-app"]),
        "missing_audience": keys.token(aud=None),
        "wrong_issuer": keys.token(iss="https://other-team.cloudflareaccess.test"),
        "missing_issuer": keys.token(iss=None),
        "expired": keys.token(iat=now - 7200, nbf=now - 7200, exp=now - 3600),
        "missing_expiry": keys.token(exp=None),
        "not_yet_valid": keys.token(nbf=now + 3600, exp=now + 7200),
    }


TOKEN_CASES = (
    "forged_signature",
    "hs256",
    "alg_none",
    "garbage",
    "wrong_audience",
    "missing_audience",
    "wrong_issuer",
    "missing_issuer",
    "expired",
    "missing_expiry",
    "not_yet_valid",
)


# @spec VAULT-ACC-003
@pytest.mark.parametrize("case", TOKEN_CASES)
async def test_invalid_access_jwt_is_refused_401(
    case: str, settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    token = _token_cases(access_keys)[case]
    async with http(settings, service, access_keys) as client:
        response = await post_initialize(client, token)
    assert_refused_before_mcp(response, 401)


# @spec VAULT-ACC-003
async def test_valid_access_jwt_is_let_through(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    async with http(settings, service, access_keys) as client:
        response = await post_initialize(client, access_keys.token(OWNER_EMAIL))
    assert response.status_code == 200


# @spec VAULT-ACC-003
async def test_access_jwt_with_audience_as_plain_string_is_let_through(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    """`aud` may be a string or a list in a JWT; the configured audience matches either."""
    async with http(settings, service, access_keys) as client:
        response = await post_initialize(client, access_keys.token(OWNER_EMAIL, aud=AUDIENCE))
    assert response.status_code == 200


# --- VAULT-ACC-004: key refetch -----------------------------------------------------------


# @spec VAULT-ACC-004
async def test_unknown_key_id_refetches_key_set_after_sixty_seconds(
    settings: Settings, service: VaultService, access_keys: AccessKeys, clock: Clock
) -> None:
    async with http(settings, service, access_keys) as client:
        first = await post_initialize(client, access_keys.token(OWNER_EMAIL))
        assert first.status_code == 200
        fetched = access_keys.fetches
        clock.advance(61)
        new_kid = access_keys.rotate()
        response = await post_initialize(client, access_keys.token(OWNER_EMAIL, kid=new_kid))
    assert response.status_code == 200
    assert access_keys.fetches == fetched + 1


# @spec VAULT-ACC-004
async def test_unknown_key_id_within_sixty_seconds_of_a_fetch_is_not_refetched(
    settings: Settings, service: VaultService, access_keys: AccessKeys, clock: Clock
) -> None:
    async with http(settings, service, access_keys) as client:
        first = await post_initialize(client, access_keys.token(OWNER_EMAIL))
        assert first.status_code == 200
        fetched = access_keys.fetches
        clock.advance(30)
        new_kid = access_keys.rotate()
        response = await post_initialize(client, access_keys.token(OWNER_EMAIL, kid=new_kid))
    assert_refused_before_mcp(response, 401)
    assert access_keys.fetches == fetched


# @spec VAULT-ACC-004
async def test_rate_limited_unknown_key_is_accepted_once_the_minute_has_passed(
    settings: Settings, service: VaultService, access_keys: AccessKeys, clock: Clock
) -> None:
    async with http(settings, service, access_keys) as client:
        await post_initialize(client, access_keys.token(OWNER_EMAIL))
        new_kid = access_keys.rotate()
        clock.advance(10)
        refused = await post_initialize(client, access_keys.token(OWNER_EMAIL, kid=new_kid))
        clock.advance(55)
        accepted = await post_initialize(client, access_keys.token(OWNER_EMAIL, kid=new_kid))
    assert refused.status_code == 401
    assert accepted.status_code == 200


# @spec VAULT-ACC-004
async def test_known_key_id_never_triggers_a_refetch(
    settings: Settings, service: VaultService, access_keys: AccessKeys, clock: Clock
) -> None:
    async with http(settings, service, access_keys) as client:
        await post_initialize(client, access_keys.token(OWNER_EMAIL))
        fetched = access_keys.fetches
        clock.advance(3600)
        response = await post_initialize(client, access_keys.token(OWNER_EMAIL))
    assert response.status_code == 200
    assert access_keys.fetches == fetched


# @spec VAULT-ACC-004, VAULT-ACC-003
async def test_key_id_absent_even_after_refetch_is_refused_401(
    settings: Settings, service: VaultService, access_keys: AccessKeys, clock: Clock
) -> None:
    async with http(settings, service, access_keys) as client:
        await post_initialize(client, access_keys.token(OWNER_EMAIL))
        fetched = access_keys.fetches
        clock.advance(61)
        response = await post_initialize(client, access_keys.token(OWNER_EMAIL, kid="kid-99"))
    assert_refused_before_mcp(response, 401)
    assert access_keys.fetches == fetched + 1


# --- VAULT-ACC-005: identity lookup -------------------------------------------------------


# @spec VAULT-ACC-005
async def test_valid_jwt_for_an_unmapped_email_is_refused_403(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    async with http(settings, service, access_keys) as client:
        response = await post_initialize(client, access_keys.token("stranger@example.com"))
    assert_refused_before_mcp(response, 403)


# @spec VAULT-ACC-005
async def test_email_lookup_is_case_insensitive(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    async with http(settings, service, access_keys) as client:
        response = await post_initialize(client, access_keys.token("OWNER@Example.COM"))
    assert response.status_code == 200


# @spec VAULT-ACC-005
async def test_email_differing_only_in_domain_is_refused_403(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    async with http(settings, service, access_keys) as client:
        response = await post_initialize(client, access_keys.token("owner@example.com.evil.test"))
    assert_refused_before_mcp(response, 403)


# --- VAULT-ACC-006: health ----------------------------------------------------------------


# @spec VAULT-ACC-006
async def test_healthz_answers_without_a_jwt_with_an_empty_body(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    async with http(settings, service, access_keys) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.content == b""


# @spec VAULT-ACC-006
async def test_healthz_ignores_an_invalid_jwt(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    async with http(settings, service, access_keys) as client:
        response = await client.get("/healthz", headers={JWT_HEADER: "not-a-jwt"})
    assert response.status_code == 200
    assert response.content == b""


# @spec VAULT-ACC-006
async def test_healthz_never_fetches_the_access_signing_keys(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    async with http(settings, service, access_keys) as client:
        await client.get("/healthz")
    assert access_keys.fetches == 0


# @spec VAULT-ACC-006, VAULT-ACC-007
async def test_healthz_answers_a_probe_addressed_by_pod_ip(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    """The host check guards the MCP endpoint; kubelet probes arrive with the pod IP as Host."""
    async with http(settings, service, access_keys) as client:
        response = await client.get("/healthz", headers={"Host": "10.42.0.7:8000"})
    assert response.status_code == 200
    assert response.content == b""


# --- VAULT-ACC-007: host check ------------------------------------------------------------


# @spec VAULT-ACC-007
async def test_mcp_request_with_unlisted_host_is_refused_421(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    async with http(settings, service, access_keys) as client:
        response = await post_initialize(
            client, access_keys.token(OWNER_EMAIL), host="attacker.example"
        )
    assert_refused_before_mcp(response, 421)


# @spec VAULT-ACC-007
async def test_mcp_request_with_listed_host_is_let_through(
    remote: Remote,
    tmp_path: Path,
    tokens: FakeTokens,
    refusals: FakeRefusals,
    clock: Clock,
    access_keys: AccessKeys,
) -> None:
    settings = make_settings(remote, tmp_path / "work", allowed_hosts=("other.test", HOST))
    service = started_service(settings, tokens, refusals, clock)
    async with http(settings, service, access_keys) as client:
        response = await post_initialize(client, access_keys.token(OWNER_EMAIL), host=HOST)
    assert response.status_code == 200


# --- VAULT-ACC-008: allowed hosts at startup ----------------------------------------------


# @spec VAULT-ACC-008
def test_empty_allowed_hosts_refuses_to_start(remote: Remote, tmp_path: Path) -> None:
    settings = make_settings(remote, tmp_path / "work", allowed_hosts=())
    with pytest.raises(StartupError):
        config.validate(settings)


# @spec VAULT-ACC-008, VAULT-ID-001
def test_valid_settings_pass_startup_validation(settings: Settings) -> None:
    config.validate(settings)


# --- VAULT-ID-001: identity map at startup ------------------------------------------------


def _identity_cases() -> dict[str, tuple[Identity, ...]]:
    admin = cast("Mode", "admin")
    return {
        "empty": (),
        "unknown_mode": (OWNER, Identity("third@example.com", admin, "Third", "t@example.com")),
        "duplicate_email_case_insensitive": (
            OWNER,
            Identity(OWNER_EMAIL.upper(), "read", "Owner Again", "again@example.com"),
        ),
        "missing_author_name": (replace(OWNER, author_name=""), READER),
        "missing_author_email": (OWNER, replace(READER, author_email="")),
    }


IDENTITY_CASES = (
    "empty",
    "unknown_mode",
    "duplicate_email_case_insensitive",
    "missing_author_name",
    "missing_author_email",
)


# @spec VAULT-ID-001
@pytest.mark.parametrize("case", IDENTITY_CASES)
def test_bad_identity_map_refuses_to_start(case: str, remote: Remote, tmp_path: Path) -> None:
    settings = make_settings(remote, tmp_path / "work", identities=_identity_cases()[case])
    with pytest.raises(StartupError):
        config.validate(settings)


# @spec VAULT-ID-001
def test_single_write_identity_is_a_valid_map(remote: Remote, tmp_path: Path) -> None:
    """The deployed map has exactly one entry, the owner in write mode."""
    config.validate(make_settings(remote, tmp_path / "work", identities=(OWNER,)))


# --- VAULT-ID-002: tools per mode ---------------------------------------------------------


# @spec VAULT-ID-002
async def test_read_caller_is_offered_only_the_read_tools(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    assert await tool_names(settings, service, access_keys, READER_EMAIL) == set(READ_TOOLS)


# @spec VAULT-ID-002
async def test_write_caller_is_offered_every_tool_including_archive(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    assert settings.archive_folder is not None
    expected = {*READ_TOOLS, *WRITE_TOOLS, ARCHIVE_TOOL}
    assert await tool_names(settings, service, access_keys, OWNER_EMAIL) == expected


# @spec VAULT-ID-002
async def test_write_caller_is_not_offered_archive_without_an_archive_folder(
    remote: Remote,
    tmp_path: Path,
    tokens: FakeTokens,
    refusals: FakeRefusals,
    clock: Clock,
    access_keys: AccessKeys,
) -> None:
    settings = make_settings(remote, tmp_path / "work", archive_folder=None)
    service = started_service(settings, tokens, refusals, clock)
    names = await tool_names(settings, service, access_keys, OWNER_EMAIL)
    assert names == {*READ_TOOLS, *WRITE_TOOLS}


# @spec VAULT-ID-002
async def test_tool_set_follows_the_mapped_identity_not_the_jwt_email_case(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    """A read caller writing their email in a different case still gets the read set."""
    names = await tool_names(settings, service, access_keys, READER_EMAIL.upper())
    assert names == set(READ_TOOLS)
