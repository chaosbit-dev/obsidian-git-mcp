"""The perimeter in front of the MCP application: host check, Cloudflare Access, identity.

Adapted from paperless-mcp's `access.py` (same author, MIT).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import anyio
import httpx2
import jwt
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response

from obsidian_git_mcp.service import AUDIT_LOGGER

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from obsidian_git_mcp.config import Identity

HEALTH_PATH = "/healthz"
JWT_HEADER = "cf-access-jwt-assertion"
# Where the middleware leaves the caller's identity for the tools.
IDENTITY_SCOPE_KEY = "obsidian_git_mcp.identity"
# A refusal line records the path, which the caller chose: bound it.
PATH_LOG_LIMIT = 200
# How long a fetched key set is trusted to be complete, in seconds.
KEY_REFETCH_INTERVAL = 60.0

audit_logger = logging.getLogger(AUDIT_LOGGER)


class AccessDenied(Exception):
    """The Access JWT is missing or does not validate. The message is the perimeter's reason."""


class AccessValidator:
    """Validates Cloudflare Access JWTs against the team's signing keys."""

    def __init__(
        self,
        team_domain: str,
        audience: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._issuer = f"https://{team_domain}"
        self._certs_url = f"{self._issuer}/cdn-cgi/access/certs"
        self._audience = audience
        self._http = httpx2.AsyncClient(transport=transport, timeout=10.0)
        self._keys: dict[str, jwt.PyJWK] = {}
        self._clock = clock
        self._fetched_at: float | None = None
        self._lock = anyio.Lock()

    async def _fetch_keys(self) -> None:
        # The attempt counts as a fetch even when it fails: an outage at Cloudflare must not
        # turn every request carrying an unknown key id into a fetch.
        self._fetched_at = self._clock()
        try:
            response = await self._http.get(self._certs_url)
            response.raise_for_status()
            body = response.json()
        except (httpx2.HTTPError, ValueError):
            raise AccessDenied("signing keys unavailable") from None
        entries = body.get("keys") if isinstance(body, dict) else None
        if not isinstance(entries, list):
            raise AccessDenied("signing keys unavailable")
        keys: dict[str, jwt.PyJWK] = {}
        for entry in entries:
            try:
                key = jwt.PyJWK.from_dict(entry)
            except (jwt.PyJWTError, TypeError, AttributeError, KeyError, ValueError):
                continue
            if key.key_id:
                keys[key.key_id] = key
        self._keys = keys

    # @spec VAULT-ACC-004
    async def _signing_key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            # A key id the cached set does not hold may be a rotation: look again, but no
            # more often than the interval, or any caller could make this server fetch on
            # every request by inventing key ids.
            if kid not in self._keys and (
                self._fetched_at is None
                or self._clock() - self._fetched_at >= KEY_REFETCH_INTERVAL
            ):
                await self._fetch_keys()
        try:
            return self._keys[kid]
        except KeyError:
            raise AccessDenied("unknown signing key") from None

    # @spec VAULT-ACC-003
    async def validate(self, token: str) -> dict[str, Any]:
        """Return the JWT's claims, or raise `AccessDenied`."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            raise AccessDenied("malformed token") from None
        if not isinstance(kid, str) or not kid:
            raise AccessDenied("token names no signing key")
        key = await self._signing_key(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": ["exp", "aud", "iss"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.PyJWTError:
            raise AccessDenied("token failed validation") from None
        return claims

    async def aclose(self) -> None:
        await self._http.aclose()


def host_allowed(host: str | None, allowed: Iterable[str]) -> bool:
    """Whether a Host header names an allowed host; a port, if any, is not part of the name."""
    if not host:
        return False
    host = host.strip().lower()
    name = host.rsplit(":", 1)[0] if not host.endswith("]") and ":" in host else host
    allowed_names = {entry.strip().lower() for entry in allowed}
    return host in allowed_names or name in allowed_names


class AccessMiddleware:
    """Everything except `/healthz` passes the host check, Access and the identity map first."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        validator: AccessValidator,
        identities: Iterable[Identity],
        allowed_hosts: Iterable[str],
        healthy: Callable[[], bool],
    ) -> None:
        self._app = app
        self._validator = validator
        self._identities: Mapping[str, Identity] = {i.email.lower(): i for i in identities}
        self._allowed_hosts = tuple(allowed_hosts)
        self._healthy = healthy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        # @spec VAULT-ACC-006, VAULT-CLONE-009
        if scope["path"] == HEALTH_PATH:
            # Kubelet probes arrive with the pod IP as Host, and carry no Access token.
            healthy = await anyio.to_thread.run_sync(self._healthy)
            await Response(status_code=200 if healthy else 503)(scope, receive, send)
            return
        headers = Headers(scope=scope)
        # @spec VAULT-ACC-007
        if not host_allowed(headers.get("host"), self._allowed_hosts):
            await self._refuse(scope, receive, send, 421, "host not allowed")
            return
        # @spec VAULT-ACC-002
        token = headers.get(JWT_HEADER)
        if not token:
            await self._refuse(scope, receive, send, 401, "missing Access token")
            return
        try:
            claims = await self._validator.validate(token)
        except AccessDenied as denied:
            await self._refuse(scope, receive, send, 401, str(denied))
            return
        # @spec VAULT-ACC-005
        email = claims.get("email")
        caller = email if isinstance(email, str) else None
        identity = self._identities.get(caller.lower()) if caller is not None else None
        if identity is None:
            await self._refuse(scope, receive, send, 403, "no identity for this caller", caller)
            return
        scope[IDENTITY_SCOPE_KEY] = identity

        # An error the MCP application answers with (an unknown path, a refused request)
        # is logged like one the perimeter answers with.
        async def watched(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] >= 400:
                status = int(message["status"])
                log_error_response(scope, status, _phrase(status), identity.email)
            await send(message)

        await self._app(scope, receive, watched)

    async def _refuse(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        status: int,
        reason: str,
        caller: str | None = None,
    ) -> None:
        log_error_response(scope, status, reason, caller)
        error = {401: "unauthorized", 403: "forbidden", 421: "misdirected"}.get(status, "refused")
        response = JSONResponse({"error": error, "detail": reason}, status_code=status)
        await response(scope, receive, send)


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return f"status {status}"


# @spec VAULT-AUDIT-004
def log_error_response(scope: Scope, status: int, reason: str, email: str | None) -> None:
    """One audit line per request answered with an error status; never the Access token."""
    line: dict[str, Any] = {
        "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "status": status,
        "method": scope.get("method"),
        "path": str(scope.get("path", ""))[:PATH_LOG_LIMIT],
        "email": email,
        "reason": reason,
    }
    audit_logger.info(json.dumps(line))
