"""Audit log: one structured line per tool call, one per error response, and nothing secret.

The audit line's wire shape is not fixed by the skeleton. These tests read an entry from a
record on the audit logger in any of three natural forms: the record's `msg` is a dict, the
formatted message is a JSON object, or the dict is attached as `extra={"audit": {...}}`. Field
names are the spec's nouns: time, email, tool, paths, count, commit, outcome (tool lines) and
time, status, method, path, email, reason (error-response lines).
"""

from __future__ import annotations

import contextlib
import json
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from obsidian_git_mcp.errors import VaultError
from obsidian_git_mcp.server import ARCHIVE_TOOL, READ_TOOLS, WRITE_TOOLS, build_app
from obsidian_git_mcp.service import AUDIT_LOGGER
from tests.conftest import GUARD_WORD, HOST, OWNER, OWNER_EMAIL, READER, READER_EMAIL

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.applications import Starlette

    from obsidian_git_mcp.config import Settings
    from obsidian_git_mcp.service import VaultService
    from tests.conftest import AccessKeys, Remote

TOOL_FIELDS = {"time", "email", "tool", "paths", "count", "commit", "outcome"}
REQUEST_FIELDS = {"time", "status", "method", "path", "email", "reason"}
UNOFFERED_TOOL = "drop_vault"


# --- helpers ---------------------------------------------------------------------------------


def _entry(record: logging.LogRecord) -> dict[str, Any]:
    audit = getattr(record, "audit", None)
    if isinstance(audit, dict):
        return audit
    if isinstance(record.msg, dict):
        return dict(record.msg)
    try:
        parsed = json.loads(record.getMessage())
    except ValueError:
        pytest.fail(f"audit record is not structured: {record.getMessage()!r}")
    if not isinstance(parsed, dict):
        pytest.fail(f"audit record is not a JSON object: {record.getMessage()!r}")
    return parsed


def audit_entries(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [_entry(r) for r in caplog.records if r.name == AUDIT_LOGGER]


def tool_entries(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [e for e in audit_entries(caplog) if "tool" in e]


def request_entries(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [e for e in audit_entries(caplog) if "status" in e]


def everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    """Every record on every logger, message and attributes, as one searchable string."""
    parts: list[str] = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(vars(record)))
    return "\n".join(parts)


@pytest.fixture
def audit(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)  # root and every logger: AUDIT-002 is about any line
    caplog.set_level(logging.DEBUG, logger=AUDIT_LOGGER)
    return caplog


def only_tool_entry(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    entries = tool_entries(caplog)
    assert len(entries) == 1, entries
    assert entries[0].keys() >= TOOL_FIELDS, entries[0]
    return entries[0]


def only_request_entry(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    entries = request_entries(caplog)
    assert len(entries) == 1, entries
    assert entries[0].keys() >= REQUEST_FIELDS, entries[0]
    return entries[0]


def client(app: Starlette, host: str = HOST) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=f"https://{host}")


@asynccontextmanager
async def mcp_session(app: Starlette, token: str) -> AsyncIterator[ClientSession]:
    async with (
        app.router.lifespan_context(app),
        client(app) as http,
    ):
        http.headers["Cf-Access-Jwt-Assertion"] = token
        async with (
            streamable_http_client(f"https://{HOST}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session


def app_for(settings: Settings, service: VaultService, access_keys: AccessKeys) -> Starlette:
    return build_app(settings, service, access_transport=access_keys.transport())


# --- VAULT-AUDIT-001: one structured line per tool call ---------------------------------------


# @spec VAULT-AUDIT-001
def test_successful_read_writes_one_line_with_caller_tool_path_and_ok(
    service: VaultService, audit: pytest.LogCaptureFixture
) -> None:
    service.read_note(OWNER, "ideas/Beta.md")

    entry = only_tool_entry(audit)
    assert entry["email"] == OWNER_EMAIL
    assert entry["tool"] == "read_note"
    assert list(entry["paths"]) == ["ideas/Beta.md"]
    assert entry["count"] == 1
    assert entry["outcome"] == "ok"
    assert entry["time"]


# @spec VAULT-AUDIT-001
def test_read_makes_no_commit_so_the_line_records_none(
    service: VaultService, audit: pytest.LogCaptureFixture
) -> None:
    service.read_note(READER, "ideas/Beta.md")

    assert only_tool_entry(audit)["commit"] is None


# @spec VAULT-AUDIT-001
def test_listing_records_the_result_count_and_the_paths_returned(
    service: VaultService, audit: pytest.LogCaptureFixture
) -> None:
    result = service.list_notes(READER, folder="notes")

    entry = only_tool_entry(audit)
    returned = [n["path"] for n in result["notes"]]
    assert entry["email"] == READER_EMAIL
    assert entry["tool"] == "list_notes"
    assert entry["count"] == len(returned) == result["total"]
    assert sorted(entry["paths"]) == sorted(returned)


# @spec VAULT-AUDIT-001
def test_write_line_records_the_commit_it_made(
    service: VaultService, remote: Remote, audit: pytest.LogCaptureFixture
) -> None:
    result = service.create_note(OWNER, "ideas/Delta.md", "# Delta\n")

    entry = only_tool_entry(audit)
    assert entry["tool"] == "create_note"
    assert entry["email"] == OWNER_EMAIL
    assert list(entry["paths"]) == ["ideas/Delta.md"]
    assert entry["commit"] == result["commit"] == remote.head()
    assert entry["outcome"] == "ok"


# @spec VAULT-AUDIT-001
def test_move_line_records_every_path_it_changed(
    service: VaultService, audit: pytest.LogCaptureFixture
) -> None:
    revision = service.read_note(OWNER, "ideas/Beta.md")["revision"]
    audit.clear()

    result = service.move_note(OWNER, "ideas/Beta.md", revision, "notes/Beta.md")

    entry = only_tool_entry(audit)
    assert entry["tool"] == "move_note"
    expected = {"ideas/Beta.md", "notes/Beta.md", *result["also_changed"]}
    assert set(entry["paths"]) == expected
    assert entry["commit"] == result["commit"]


# @spec VAULT-AUDIT-001
def test_failed_call_writes_one_line_with_the_failure_code(
    service: VaultService, audit: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(VaultError) as raised:
        service.read_note(READER, "ideas/Nowhere.md")
    assert raised.value.code == "not_found"

    entry = only_tool_entry(audit)
    assert entry["email"] == READER_EMAIL
    assert entry["tool"] == "read_note"
    assert entry["outcome"] == "not_found"
    assert entry["commit"] is None


# @spec VAULT-AUDIT-001
def test_failed_write_records_no_commit_and_the_stale_revision_code(
    service: VaultService, remote: Remote, audit: pytest.LogCaptureFixture
) -> None:
    revision = service.read_note(OWNER, "ideas/Beta.md")["revision"]
    remote.mac_commit({"ideas/Beta.md": "# Beta\n\nChanged on the Mac.\n"})
    head = remote.head()
    audit.clear()

    with pytest.raises(VaultError) as raised:
        service.edit_note(OWNER, "ideas/Beta.md", revision, body="# Beta\n\nMine.\n")
    assert raised.value.code == "stale_revision"

    entry = only_tool_entry(audit)
    assert entry["tool"] == "edit_note"
    assert entry["outcome"] == "stale_revision"
    assert entry["commit"] is None
    assert remote.head() == head


# @spec VAULT-AUDIT-001
def test_each_call_writes_its_own_line(
    service: VaultService, audit: pytest.LogCaptureFixture
) -> None:
    service.read_note(READER, "ideas/Beta.md")
    service.list_notes(READER)
    with pytest.raises(VaultError):
        service.read_note(READER, "ideas/Nowhere.md")

    assert [e["tool"] for e in tool_entries(audit)] == ["read_note", "list_notes", "read_note"]


# --- VAULT-AUDIT-002: nothing secret in any line ----------------------------------------------


# @spec VAULT-AUDIT-002
def test_note_body_read_is_not_logged(
    service: VaultService, audit: pytest.LogCaptureFixture
) -> None:
    result = service.read_note(OWNER, "ideas/Beta.md")
    assert "Design text." in result["body"]

    logged = everything_logged(audit)
    assert "Design text." not in logged
    assert "Notes text." not in logged


# @spec VAULT-AUDIT-002
def test_frontmatter_value_read_is_not_logged(
    service: VaultService, audit: pytest.LogCaptureFixture
) -> None:
    result = service.read_note(OWNER, "ideas/Alpha.md")
    assert "Gonk/K3s" in result["frontmatter"]["area"]

    assert "Gonk/K3s" not in everything_logged(audit)


# @spec VAULT-AUDIT-002
def test_body_and_field_values_written_are_not_logged(
    service: VaultService, remote: Remote, audit: pytest.LogCaptureFixture
) -> None:
    service.create_note(
        OWNER, "ideas/Delta.md", "Body-sentinel-7f3a\n", fields={"status": "field-sentinel-9c1e"}
    )
    assert "Body-sentinel-7f3a" in (remote.show("ideas/Delta.md") or "")

    logged = everything_logged(audit)
    assert "Body-sentinel-7f3a" not in logged
    assert "field-sentinel-9c1e" not in logged


# @spec VAULT-AUDIT-002
def test_appended_text_is_not_logged(
    service: VaultService, audit: pytest.LogCaptureFixture
) -> None:
    service.append_to_note(OWNER, "ideas/Beta.md", "Appended-sentinel-21bd", under_heading="Notes")

    assert "Appended-sentinel-21bd" not in everything_logged(audit)


# @spec VAULT-AUDIT-002
def test_search_query_is_not_logged(
    service: VaultService, audit: pytest.LogCaptureFixture
) -> None:
    service.search_notes(READER, query="Query-sentinel-5e0d")

    assert len(tool_entries(audit)) == 1
    assert "Query-sentinel-5e0d" not in everything_logged(audit)


# @spec VAULT-AUDIT-002
def test_guard_refusal_logs_neither_the_pattern_nor_the_matching_text(
    service: VaultService, remote: Remote, audit: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(VaultError) as raised:
        service.create_note(OWNER, "ideas/Delta.md", f"About {GUARD_WORD}.\n")
    assert raised.value.code == "refused_guard"
    assert remote.show("ideas/Delta.md") is None

    entry = only_tool_entry(audit)
    assert entry["outcome"] == "refused_guard"
    logged = everything_logged(audit)
    assert GUARD_WORD not in logged
    assert r"\b" not in logged


# --- VAULT-AUDIT-003: refused before any tool runs (through the app) --------------------------


# @spec VAULT-AUDIT-003
async def test_unoffered_tool_is_logged_as_unknown(
    settings: Settings,
    service: VaultService,
    access_keys: AccessKeys,
    audit: pytest.LogCaptureFixture,
) -> None:
    assert UNOFFERED_TOOL not in (*READ_TOOLS, *WRITE_TOOLS, ARCHIVE_TOOL)
    app = app_for(settings, service, access_keys)

    async with mcp_session(app, access_keys.token(OWNER_EMAIL)) as session:
        with contextlib.suppress(Exception):
            await session.call_tool(UNOFFERED_TOOL, {"note": "ideas/Beta.md"})

    entry = only_tool_entry(audit)
    assert entry["tool"] == "(unknown)"
    assert entry["email"] == OWNER_EMAIL
    assert entry["outcome"] != "ok"
    assert entry["commit"] is None
    assert UNOFFERED_TOOL not in everything_logged(audit)


# @spec VAULT-AUDIT-003
async def test_write_tool_refused_for_reader_still_writes_a_line(
    settings: Settings,
    service: VaultService,
    remote: Remote,
    access_keys: AccessKeys,
    audit: pytest.LogCaptureFixture,
) -> None:
    head = remote.head()
    app = app_for(settings, service, access_keys)

    async with mcp_session(app, access_keys.token(READER_EMAIL)) as session:
        with contextlib.suppress(Exception):
            await session.call_tool("create_note", {"path": "ideas/Delta.md", "body": "x\n"})

    entry = only_tool_entry(audit)
    assert entry["email"] == READER_EMAIL
    assert entry["outcome"] != "ok"
    assert entry["commit"] is None
    assert remote.head() == head


# --- VAULT-AUDIT-004: error responses -----------------------------------------------------------


# @spec VAULT-AUDIT-004
async def test_missing_token_401_writes_one_line_without_email(
    settings: Settings,
    service: VaultService,
    access_keys: AccessKeys,
    audit: pytest.LogCaptureFixture,
) -> None:
    async with client(app_for(settings, service, access_keys)) as http:
        response = await http.post("/mcp", json={})
    assert response.status_code == 401

    entry = only_request_entry(audit)
    assert entry["status"] == 401
    assert entry["method"] == "POST"
    assert entry["path"] == "/mcp"
    assert entry["email"] is None
    assert entry["reason"]
    assert entry["time"]


# @spec VAULT-AUDIT-004
async def test_bad_signature_401_never_logs_the_token(
    settings: Settings,
    service: VaultService,
    access_keys: AccessKeys,
    audit: pytest.LogCaptureFixture,
) -> None:
    forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = access_keys.token(OWNER_EMAIL, key=forged_key)

    async with client(app_for(settings, service, access_keys)) as http:
        response = await http.post("/mcp", json={}, headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 401

    entry = only_request_entry(audit)
    assert entry["status"] == 401
    assert entry["email"] is None  # the email claim was never validated
    assert entry["reason"]
    logged = everything_logged(audit)
    for segment in token.split("."):
        assert segment not in logged


# @spec VAULT-AUDIT-004
async def test_unknown_identity_403_logs_the_validated_email_and_not_the_token(
    settings: Settings,
    service: VaultService,
    access_keys: AccessKeys,
    audit: pytest.LogCaptureFixture,
) -> None:
    token = access_keys.token("stranger@example.com")

    async with client(app_for(settings, service, access_keys)) as http:
        response = await http.get("/mcp", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 403

    entry = only_request_entry(audit)
    assert entry["status"] == 403
    assert entry["method"] == "GET"
    assert entry["path"] == "/mcp"
    assert entry["email"] == "stranger@example.com"
    assert entry["reason"]
    logged = everything_logged(audit)
    for segment in token.split("."):
        assert segment not in logged


# @spec VAULT-AUDIT-004
async def test_wrong_host_421_writes_one_line(
    settings: Settings,
    service: VaultService,
    access_keys: AccessKeys,
    audit: pytest.LogCaptureFixture,
) -> None:
    token = access_keys.token(OWNER_EMAIL)

    app = app_for(settings, service, access_keys)
    async with client(app, host="evil.test") as http:
        response = await http.post("/mcp", json={}, headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 421

    entry = only_request_entry(audit)
    assert entry["status"] == 421
    assert entry["method"] == "POST"
    assert entry["path"] == "/mcp"
    assert entry["reason"]
    for segment in token.split("."):
        assert segment not in everything_logged(audit)


# @spec VAULT-AUDIT-004
async def test_each_error_response_writes_its_own_line(
    settings: Settings,
    service: VaultService,
    access_keys: AccessKeys,
    audit: pytest.LogCaptureFixture,
) -> None:
    async with client(app_for(settings, service, access_keys)) as http:
        first = await http.post("/mcp", json={})
        second = await http.post(
            "/mcp",
            json={},
            headers={"Cf-Access-Jwt-Assertion": access_keys.token("stranger@example.com")},
        )
    assert (first.status_code, second.status_code) == (401, 403)

    assert [e["status"] for e in request_entries(audit)] == [401, 403]
