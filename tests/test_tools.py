"""The tools themselves: what each one does to the remote and what it returns.

Name resolution, link rewriting, refusals and the write rules have their own files; these tests
address notes by path and pick targets that no pin, guard or link touches unless the test is
about that.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from obsidian_git_mcp.errors import VaultError
from obsidian_git_mcp.server import ARCHIVE_TOOL, READ_TOOLS, WRITE_TOOLS, build_app
from obsidian_git_mcp.service import VaultService
from tests.conftest import HOST, OWNER, OWNER_EMAIL, SEED, git, make_settings

if TYPE_CHECKING:
    from pathlib import Path

    from obsidian_git_mcp.config import Settings
    from tests.conftest import AccessKeys, Clock, FakeRefusals, FakeTokens, Remote


def blob(remote: Remote, path: str) -> str:
    """The git blob id of `path` at the remote branch's tip: what `revision` must equal."""
    return git(remote.bare, "rev-parse", f"{remote.branch}:{path}").strip()


def body_of(seeded: str) -> str:
    """A seeded note's text after its frontmatter block."""
    return seeded.split("---\n", 2)[2]


# --- read_note ------------------------------------------------------------------------------


# @spec VAULT-TOOL-001
def test_read_note_returns_path_frontmatter_and_body(service: VaultService) -> None:
    result = service.read_note(OWNER, "ideas/Alpha.md")

    assert result["path"] == "ideas/Alpha.md"
    assert result["frontmatter"] == {
        "status": "idea",
        "area": ["HA", "Gonk/K3s"],
        "priority": "P2",
        "related": "[[Gamma]]",
    }
    assert result["body"] == body_of(SEED["ideas/Alpha.md"])


# @spec VAULT-TOOL-001
def test_read_note_returns_note_without_frontmatter_whole_as_body(service: VaultService) -> None:
    result = service.read_note(OWNER, "ideas/Beta.md")

    assert result["frontmatter"] == {}
    assert result["body"] == SEED["ideas/Beta.md"]


# @spec VAULT-TOOL-001
def test_read_note_revision_is_the_git_blob_id(service: VaultService, remote: Remote) -> None:
    result = service.read_note(OWNER, "notes/Gamma.md")

    assert result["revision"] == blob(remote, "notes/Gamma.md")


# @spec VAULT-TOOL-001
def test_read_note_lists_resolvable_notes_it_links_to(service: VaultService) -> None:
    """Alpha links to Beta in the body and to Gamma from both frontmatter and a markdown link."""
    result = service.read_note(OWNER, "ideas/Alpha.md")

    assert set(result["links_to"]) == {"ideas/Beta.md", "notes/Gamma.md"}


# @spec VAULT-TOOL-001
def test_read_note_lists_resolvable_notes_linking_to_it(service: VaultService) -> None:
    result = service.read_note(OWNER, "ideas/Beta.md")

    assert set(result["linked_from"]) == {"ideas/Alpha.md", "notes/Gamma.md"}
    assert list(result["links_to"]) == []


# @spec VAULT-TOOL-001
def test_read_note_link_lists_leave_out_unresolvable_notes(
    service: VaultService, remote: Remote
) -> None:
    """A note in the trash or a dot-directory is not resolvable, in either direction."""
    remote.mac_commit(
        {
            "notes/Linker.md": "# Linker\n\n[[Secret]] and [[Binned]] and [[Deep]]\n",
            "trash/Binned.md": "# Binned\n\n[[Linker]]\n",
            ".hidden/Other.md": "# Other\n\n[[Linker]]\n",
        }
    )

    result = service.read_note(OWNER, "notes/Linker.md")

    assert set(result["links_to"]) == {"Deep.md"}
    assert list(result["linked_from"]) == []


# --- create_note ----------------------------------------------------------------------------


# @spec VAULT-TOOL-002
def test_create_note_writes_body_at_the_given_path(service: VaultService, remote: Remote) -> None:
    service.create_note(OWNER, "notes/Delta.md", "# Delta\n\nFresh.\n")

    assert remote.show("notes/Delta.md") == "# Delta\n\nFresh.\n"


# @spec VAULT-TOOL-002
def test_create_note_writes_given_fields_as_frontmatter(
    service: VaultService, remote: Remote
) -> None:
    service.create_note(OWNER, "notes/Delta.md", "# Delta\n", {"status": "idea"})

    assert remote.show("notes/Delta.md") == "---\nstatus: idea\n---\n# Delta\n"


# @spec VAULT-TOOL-002
def test_create_note_creates_missing_parent_folders(service: VaultService, remote: Remote) -> None:
    service.create_note(OWNER, "projects/new/deeper/Idea.md", "# Idea\n")

    assert remote.show("projects/new/deeper/Idea.md") == "# Idea\n"


# @spec VAULT-TOOL-002
def test_create_note_refuses_a_path_equivalent_to_an_existing_note(
    service: VaultService, remote: Remote
) -> None:
    before = remote.head()

    with pytest.raises(VaultError) as exc:
        service.create_note(OWNER, "IDEAS/alpha.md", "# Clash\n")

    assert exc.value.code == "confinement"
    assert remote.head() == before
    assert remote.show("ideas/Alpha.md") == SEED["ideas/Alpha.md"]


# --- append_to_note -------------------------------------------------------------------------


# @spec VAULT-TOOL-003
def test_append_to_note_adds_text_at_the_end(service: VaultService, remote: Remote) -> None:
    service.append_to_note(OWNER, "Deep.md", "Appended line.\n")

    assert remote.show("Deep.md") == "# Deep at root\nAppended line.\n"


# @spec VAULT-TOOL-003
def test_append_to_note_under_heading_adds_text_at_end_of_that_section(
    service: VaultService, remote: Remote
) -> None:
    """Design's section includes its ### Sub, so the text lands after Sub, before ## Notes."""
    original = SEED["ideas/Beta.md"]

    service.append_to_note(OWNER, "ideas/Beta.md", "Appended.\n", under_heading="Design")

    text = remote.show("ideas/Beta.md")
    assert text is not None
    assert text.count("Appended.") == 1
    assert text.index("Sub text.") < text.index("Appended.") < text.index("## Notes")
    assert text.startswith(original[: original.index("Sub text.\n") + len("Sub text.\n")])
    assert text.endswith(original[original.index("## Notes") :])


# @spec VAULT-TOOL-003
def test_append_to_note_takes_no_revision() -> None:
    params = inspect.signature(VaultService.append_to_note).parameters

    assert "revision" not in params


# @spec VAULT-TOOL-003
def test_append_to_note_succeeds_after_an_outside_edit_to_the_note(
    service: VaultService, remote: Remote
) -> None:
    """Nothing the caller saw is replaced, so a change made on the Mac does not block it."""
    service.read_note(OWNER, "Deep.md")
    remote.mac_commit({"Deep.md": "# Deep at root\nEdited on the Mac.\n"})

    service.append_to_note(OWNER, "Deep.md", "From the phone.\n")

    assert remote.show("Deep.md") == "# Deep at root\nEdited on the Mac.\nFrom the phone.\n"


# --- edit_note ------------------------------------------------------------------------------


# @spec VAULT-TOOL-004
def test_edit_note_body_replaces_whole_body_and_keeps_frontmatter(
    service: VaultService, remote: Remote
) -> None:
    rev = blob(remote, "notes/Gamma.md")

    service.edit_note(OWNER, "notes/Gamma.md", rev, body="# Gamma\n\nRewritten.\n")

    assert remote.show("notes/Gamma.md") == (
        "---\nstatus: reference\ntags: [x, y]\nn: 1\n---\n# Gamma\n\nRewritten.\n"
    )


# @spec VAULT-TOOL-004
def test_edit_note_body_with_section_replaces_only_that_sections_body(
    service: VaultService, remote: Remote
) -> None:
    original = SEED["ideas/Beta.md"]
    rev = blob(remote, "ideas/Beta.md")

    service.edit_note(OWNER, "ideas/Beta.md", rev, body="Replaced.\n", section="Notes")

    text = remote.show("ideas/Beta.md")
    assert text is not None
    assert "Notes text." not in text
    assert text.index("## Notes") < text.index("Replaced.") < text.index("## Dup")
    assert text.startswith(original[: original.index("## Notes\n") + len("## Notes\n")])
    assert text.endswith(original[original.index("## Dup") :])


# @spec VAULT-TOOL-004
def test_edit_note_fields_sets_frontmatter_and_leaves_body(
    service: VaultService, remote: Remote
) -> None:
    rev = blob(remote, "notes/Gamma.md")

    service.edit_note(OWNER, "notes/Gamma.md", rev, fields={"status": "stale"})

    assert remote.show("notes/Gamma.md") == SEED["notes/Gamma.md"].replace(
        "status: reference", "status: stale"
    )


# @spec VAULT-TOOL-004
def test_edit_note_body_and_fields_together_apply_both_in_one_commit(
    service: VaultService, remote: Remote
) -> None:
    rev = blob(remote, "notes/Gamma.md")
    commits = remote.commits()

    service.edit_note(OWNER, "notes/Gamma.md", rev, body="New.\n", fields={"n": 2})

    assert remote.show("notes/Gamma.md") == (
        "---\nstatus: reference\ntags: [x, y]\nn: 2\n---\nNew.\n"
    )
    assert remote.commits() == commits + 1


# --- move_note ------------------------------------------------------------------------------


# @spec VAULT-TOOL-005
def test_move_note_moves_the_note_to_the_given_path(service: VaultService, remote: Remote) -> None:
    rev = blob(remote, "Deep.md")

    service.move_note(OWNER, "Deep.md", rev, "notes/Shallow.md")

    assert remote.show("Deep.md") is None
    assert remote.show("notes/Shallow.md") == SEED["Deep.md"]


# @spec VAULT-TOOL-006
@pytest.mark.parametrize(
    "destination",
    ["trash/Deep.md", "trash/nested/Deep.md", "archive/Deep.md", "archive/ideas/Deep.md"],
)
def test_move_note_refuses_a_destination_in_trash_or_archive(
    service: VaultService, remote: Remote, destination: str
) -> None:
    before = remote.head()

    with pytest.raises(VaultError) as exc:
        service.move_note(OWNER, "Deep.md", blob(remote, "Deep.md"), destination)

    assert exc.value.code == "confinement"
    assert remote.head() == before


# @spec VAULT-TOOL-006
def test_move_note_refuses_the_trash_folder_under_a_different_case(
    service: VaultService, remote: Remote
) -> None:
    """Paths compare by equivalence, so `Trash/` is the trash on the operator's Mac."""
    with pytest.raises(VaultError) as exc:
        service.move_note(OWNER, "Deep.md", blob(remote, "Deep.md"), "Trash/Deep.md")

    assert exc.value.code == "confinement"


# @spec VAULT-TOOL-006
def test_move_note_refuses_a_configured_trash_folder(
    remote: Remote,
    tmp_path: Path,
    tokens: FakeTokens,
    refusals: FakeRefusals,
    clock: Clock,
) -> None:
    svc = VaultService(
        make_settings(remote, tmp_path / "work", trash_folder="bin"), tokens, refusals, clock=clock
    )
    svc.start()

    with pytest.raises(VaultError) as exc:
        svc.move_note(OWNER, "Deep.md", blob(remote, "Deep.md"), "bin/Deep.md")

    assert exc.value.code == "confinement"


# --- delete_note ----------------------------------------------------------------------------


# @spec VAULT-TOOL-007
def test_delete_note_moves_note_into_trash_at_its_vault_relative_path(
    service: VaultService, remote: Remote
) -> None:
    rev = blob(remote, "ideas/sub/Deep.md")

    service.delete_note(OWNER, "ideas/sub/Deep.md", rev)

    assert remote.show("ideas/sub/Deep.md") is None
    assert remote.show("trash/ideas/sub/Deep.md") == SEED["ideas/sub/Deep.md"]


# @spec VAULT-TOOL-007
def test_delete_note_creates_missing_folders_under_an_existing_trash(
    service: VaultService, remote: Remote
) -> None:
    remote.mac_commit({"trash/Other.md": "# Other\n"})

    service.delete_note(OWNER, "notes/Twin.md", blob(remote, "notes/Twin.md"))

    assert remote.show("trash/notes/Twin.md") == SEED["notes/Twin.md"]
    assert remote.show("trash/Other.md") == "# Other\n"


# @spec VAULT-TOOL-008
def test_delete_note_refuses_a_note_already_in_the_trash(
    service: VaultService, remote: Remote
) -> None:
    remote.mac_commit({"trash/Old.md": "# Old\n"})
    before = remote.head()

    with pytest.raises(VaultError) as exc:
        service.delete_note(OWNER, "trash/Old.md", blob(remote, "trash/Old.md"))

    assert exc.value.code == "confinement"
    assert remote.head() == before
    assert remote.show("trash/Old.md") == "# Old\n"


# --- archive_note ---------------------------------------------------------------------------


# @spec VAULT-TOOL-009
def test_archive_note_moves_note_into_archive_at_its_vault_relative_path(
    service: VaultService, remote: Remote
) -> None:
    service.archive_note(OWNER, "ideas/Twin.md", blob(remote, "ideas/Twin.md"))

    assert remote.show("ideas/Twin.md") is None
    assert remote.show("archive/ideas/Twin.md") == SEED["ideas/Twin.md"]


# @spec VAULT-TOOL-009
def test_archive_note_sets_the_given_fields_in_the_same_commit(
    service: VaultService, remote: Remote
) -> None:
    commits = remote.commits()

    service.archive_note(
        OWNER, "notes/Gamma.md", blob(remote, "notes/Gamma.md"), {"status": "archived"}
    )

    # Gamma's relative link back to Alpha gains a level, as VAULT-LINK-005 requires.
    assert remote.show("archive/notes/Gamma.md") == SEED["notes/Gamma.md"].replace(
        "status: reference", "status: archived"
    ).replace("[back](../ideas/Alpha.md)", "[back](../../ideas/Alpha.md)")
    assert remote.commits() == commits + 1


# --- trash and archive suffixes -------------------------------------------------------------


# @spec VAULT-TOOL-010
def test_delete_note_onto_a_taken_trash_path_appends_1(
    service: VaultService, remote: Remote
) -> None:
    remote.mac_commit({"trash/Deep.md": "# Earlier deep\n"})

    service.delete_note(OWNER, "Deep.md", blob(remote, "Deep.md"))

    assert remote.show("trash/Deep.md") == "# Earlier deep\n"
    assert remote.show("trash/Deep 1.md") == SEED["Deep.md"]


# @spec VAULT-TOOL-010
def test_delete_note_uses_the_first_free_suffix(service: VaultService, remote: Remote) -> None:
    remote.mac_commit(
        {"trash/Deep.md": "# zero\n", "trash/Deep 1.md": "# one\n", "trash/Deep 3.md": "# three\n"}
    )

    service.delete_note(OWNER, "Deep.md", blob(remote, "Deep.md"))

    assert remote.show("trash/Deep 2.md") == SEED["Deep.md"]
    assert remote.show("trash/Deep 3.md") == "# three\n"


# @spec VAULT-TOOL-010
def test_archive_note_onto_a_taken_archive_path_appends_1(
    service: VaultService, remote: Remote
) -> None:
    remote.mac_commit({"archive/ideas/Twin.md": "# Older twin\n"})

    service.archive_note(OWNER, "ideas/Twin.md", blob(remote, "ideas/Twin.md"))

    assert remote.show("archive/ideas/Twin.md") == "# Older twin\n"
    assert remote.show("archive/ideas/Twin 1.md") == SEED["ideas/Twin.md"]


# --- trash default --------------------------------------------------------------------------


# @spec VAULT-TOOL-011
def test_trash_folder_defaults_to_trash(remote: Remote, tmp_path: Path) -> None:
    settings = make_settings(remote, tmp_path / "work")

    assert settings.trash_folder == "trash"


# @spec VAULT-TOOL-011
def test_delete_note_uses_a_configured_trash_folder_instead_of_trash(
    remote: Remote,
    tmp_path: Path,
    tokens: FakeTokens,
    refusals: FakeRefusals,
    clock: Clock,
) -> None:
    svc = VaultService(
        make_settings(remote, tmp_path / "work", trash_folder="bin"), tokens, refusals, clock=clock
    )
    svc.start()

    svc.delete_note(OWNER, "Deep.md", blob(remote, "Deep.md"))

    assert remote.show("bin/Deep.md") == SEED["Deep.md"]
    assert remote.show("trash/Deep.md") is None


# --- what a write returns -------------------------------------------------------------------


# @spec VAULT-TOOL-012
def test_create_note_returns_path_new_revision_and_commit(
    service: VaultService, remote: Remote
) -> None:
    result = service.create_note(OWNER, "notes/Delta.md", "# Delta\n")

    assert result["path"] == "notes/Delta.md"
    assert result["revision"] == blob(remote, "notes/Delta.md")
    assert result["commit"] == remote.head()


# @spec VAULT-TOOL-012
def test_move_note_returns_the_new_path_and_its_revision(
    service: VaultService, remote: Remote
) -> None:
    result = service.move_note(OWNER, "Deep.md", blob(remote, "Deep.md"), "notes/Shallow.md")

    assert result["path"] == "notes/Shallow.md"
    assert result["revision"] == blob(remote, "notes/Shallow.md")
    assert result["commit"] == remote.head()


# @spec VAULT-TOOL-012
def test_delete_note_returns_the_trash_path_and_its_revision(
    service: VaultService, remote: Remote
) -> None:
    result = service.delete_note(OWNER, "Deep.md", blob(remote, "Deep.md"))

    assert result["path"] == "trash/Deep.md"
    assert result["revision"] == blob(remote, "trash/Deep.md")
    assert result["commit"] == remote.head()


# @spec VAULT-TOOL-012
def test_returned_revision_chains_into_a_second_edit_without_a_read(
    service: VaultService, remote: Remote
) -> None:
    first = service.edit_note(
        OWNER, "notes/Gamma.md", blob(remote, "notes/Gamma.md"), fields={"n": 2}
    )

    second = service.edit_note(OWNER, "notes/Gamma.md", first["revision"], fields={"n": 3})

    assert second["revision"] == blob(remote, "notes/Gamma.md")
    assert second["commit"] == remote.head()
    assert "n: 3\n" in (remote.show("notes/Gamma.md") or "")


# --- the MCP application --------------------------------------------------------------------


async def mcp_session_facts(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> tuple[str | None, set[str]]:
    """Connect as the owner over streamable HTTP; return (instructions, registered tool names)."""
    app = build_app(settings, service, access_transport=access_keys.transport())
    headers = {"Cf-Access-Jwt-Assertion": access_keys.token(OWNER_EMAIL)}
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=f"https://{HOST}", headers=headers
        ) as http,
        Client(
            streamable_http_client(f"https://{HOST}/mcp", http_client=http), mode="legacy"
        ) as client,
    ):
        tools = await client.list_tools()
        return client.instructions, {tool.name for tool in tools.tools}


# @spec VAULT-TOOL-009
async def test_archive_note_is_registered_when_an_archive_folder_is_configured(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    assert settings.archive_folder == "archive"

    _, tools = await mcp_session_facts(settings, service, access_keys)

    assert tools == {*READ_TOOLS, *WRITE_TOOLS, ARCHIVE_TOOL}


# @spec VAULT-TOOL-009
async def test_archive_note_is_not_registered_without_an_archive_folder(
    remote: Remote,
    tmp_path: Path,
    tokens: FakeTokens,
    refusals: FakeRefusals,
    clock: Clock,
    access_keys: AccessKeys,
) -> None:
    settings = make_settings(remote, tmp_path / "work", archive_folder=None)
    svc = VaultService(settings, tokens, refusals, clock=clock)
    svc.start()

    _, tools = await mcp_session_facts(settings, svc, access_keys)

    assert ARCHIVE_TOOL not in tools
    assert tools == {*READ_TOOLS, *WRITE_TOOLS}


# @spec VAULT-TOOL-013
async def test_mcp_instructions_say_returned_content_is_data_not_instructions(
    settings: Settings, service: VaultService, access_keys: AccessKeys
) -> None:
    instructions, _ = await mcp_session_facts(settings, service, access_keys)

    assert instructions
    text = instructions.casefold()
    for term in ("body", "frontmatter", "name", "data", "never", "instruction"):
        assert term in text, term
