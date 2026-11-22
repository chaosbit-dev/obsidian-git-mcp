"""The HTTP app: Access perimeter, host check, health, and the MCP tools per caller mode."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx2
    from starlette.applications import Starlette

    from obsidian_git_mcp.config import Settings
    from obsidian_git_mcp.service import VaultService

READ_TOOLS = ("search_notes", "read_note", "list_notes")
WRITE_TOOLS = ("create_note", "append_to_note", "edit_note", "move_note", "delete_note")
ARCHIVE_TOOL = "archive_note"


def build_app(
    settings: Settings,
    service: VaultService,
    *,
    access_transport: httpx2.AsyncBaseTransport | None = None,
) -> Starlette:
    """The ASGI app. `access_transport` lets tests serve the Access signing keys."""
    raise NotImplementedError


def main() -> None:
    raise NotImplementedError
