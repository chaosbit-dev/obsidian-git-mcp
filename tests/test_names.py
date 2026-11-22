"""Names and addressing (VAULT-NAME-*): how a note argument resolves, and which paths a change
may produce.

Equivalence is NFC normalisation plus case folding. The seeded vault supplies the cases:
Alpha/Beta resolve uniquely, Twin is ambiguous at one depth, Deep exists at two depths, and
.hidden/Secret.md is outside the resolvable set.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Any

import pytest

from obsidian_git_mcp.errors import VaultError
from tests.conftest import OWNER, git

if TYPE_CHECKING:
    from obsidian_git_mcp.service import VaultService
    from tests.conftest import Clock, Remote

CAFE_NFC = unicodedata.normalize("NFC", "Café")
CAFE_NFD = unicodedata.normalize("NFD", "Café")
assert CAFE_NFC != CAFE_NFD


def outside(remote: Remote, clock: Clock, files: dict[str, str | None]) -> None:
    """Commit from the Mac, then move past the read freshness window so the server sees it."""
    remote.mac_commit(files)
    clock.advance(60)


def blob(remote: Remote, path: str) -> str:
    """The note's current revision (git blob id) on the remote branch."""
    return git(remote.bare, "rev-parse", f"{remote.branch}:{path}").strip()


def tree(remote: Remote) -> list[str]:
    """Every path on the remote branch, unquoted.

    `Remote.files_lines()` returns git's C-quoted form for non-ASCII paths, so it cannot be
    compared with a path holding an accent.
    """
    out = git(remote.bare, "ls-tree", "-r", "-z", "--name-only", remote.branch)
    return [p for p in out.split("\0") if p]


def strings_in(value: Any) -> set[str]:
    """Every string anywhere inside an error detail, whatever its shape."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        found: set[str] = set()
        for k, v in value.items():
            found |= strings_in(k) | strings_in(v)
        return found
    if isinstance(value, list | tuple | set | frozenset):
        found = set()
        for item in value:
            found |= strings_in(item)
        return found
    return set()


def commit_nfd_cafe(remote: Remote, clock: Clock) -> str:
    """Store notes/Café.md with an NFD file name, as some tools on macOS write it.

    git on macOS precomposes to NFC by default, so the Mac clone is told not to; the stored
    path is checked so the test cannot silently degrade into an NFC-only case.
    """
    git(remote.mac, "config", "core.precomposeunicode", "false")
    path = f"notes/{CAFE_NFD}.md"
    outside(remote, clock, {path: "# Café\n"})
    assert path in tree(remote)
    return path


# --- VAULT-NAME-001: vault-relative paths ----------------------------------------------------


# @spec VAULT-NAME-001
def test_path_resolves_to_the_note_at_that_path(service: VaultService) -> None:
    result = service.read_note(OWNER, "ideas/Alpha.md")
    assert result["path"] == "ideas/Alpha.md"


# @spec VAULT-NAME-001
def test_path_resolves_case_insensitively_to_the_stored_path(service: VaultService) -> None:
    result = service.read_note(OWNER, "IDEAS/alpha.MD")
    assert result["path"] == "ideas/Alpha.md"


# @spec VAULT-NAME-001
def test_nfc_path_resolves_to_a_note_stored_nfd(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    stored = commit_nfd_cafe(remote, clock)
    result = service.read_note(OWNER, f"notes/{CAFE_NFC}.md")
    assert result["path"] == stored


# @spec VAULT-NAME-001
def test_nfd_path_resolves_to_a_note_stored_nfc(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    outside(remote, clock, {f"notes/{CAFE_NFC}.md": "# Café\n"})
    result = service.read_note(OWNER, f"notes/{CAFE_NFD}.md")
    assert result["path"] == f"notes/{CAFE_NFC}.md"


# @spec VAULT-NAME-001
def test_changing_call_resolves_an_equivalent_path_to_the_existing_note(
    service: VaultService, remote: Remote
) -> None:
    before = remote.files_lines()
    service.append_to_note(OWNER, "IDEAS/beta.md", "Appended line.\n")
    assert remote.files_lines() == before
    beta = remote.show("ideas/Beta.md")
    assert beta is not None
    assert beta.rstrip("\n").endswith("Appended line.")


# --- VAULT-NAME-002: bare names --------------------------------------------------------------


# @spec VAULT-NAME-002
def test_name_resolves_to_the_unique_note_with_that_name(service: VaultService) -> None:
    result = service.read_note(OWNER, "Alpha")
    assert result["path"] == "ideas/Alpha.md"


# @spec VAULT-NAME-002
def test_name_resolves_case_insensitively(service: VaultService) -> None:
    result = service.read_note(OWNER, "gAMMA")
    assert result["path"] == "notes/Gamma.md"


# @spec VAULT-NAME-002
def test_nfd_name_resolves_to_a_note_named_nfc(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    outside(remote, clock, {f"notes/{CAFE_NFC}.md": "# Café\n"})
    result = service.read_note(OWNER, CAFE_NFD)
    assert result["path"] == f"notes/{CAFE_NFC}.md"


# @spec VAULT-NAME-002
def test_nfc_name_resolves_to_a_note_named_nfd(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    stored = commit_nfd_cafe(remote, clock)
    result = service.read_note(OWNER, CAFE_NFC.upper())
    assert result["path"] == stored


# @spec VAULT-NAME-002
def test_shared_name_resolves_to_the_shortest_path(service: VaultService) -> None:
    """Deep.md (root) and ideas/sub/Deep.md share the name; the root one wins."""
    result = service.read_note(OWNER, "Deep")
    assert result["path"] == "Deep.md"


# @spec VAULT-NAME-002
def test_shortest_path_ignores_a_shallower_note_outside_the_resolvable_set(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    """A dot-directory note is not resolvable, so it cannot win on depth."""
    outside(remote, clock, {".h/Gamma.md": "# hidden gamma\n"})
    result = service.read_note(OWNER, "Gamma")
    assert result["path"] == "notes/Gamma.md"


# --- VAULT-NAME-003: ambiguity ---------------------------------------------------------------


# @spec VAULT-NAME-003
def test_name_shared_at_the_shortest_depth_is_ambiguous(service: VaultService) -> None:
    with pytest.raises(VaultError) as exc:
        service.read_note(OWNER, "Twin")
    assert exc.value.code == "ambiguous"


# @spec VAULT-NAME-003
def test_ambiguous_error_lists_every_candidate_path(service: VaultService) -> None:
    with pytest.raises(VaultError) as exc:
        service.read_note(OWNER, "twin")
    found = strings_in(exc.value.detail)
    assert {"ideas/Twin.md", "notes/Twin.md"} <= found


# @spec VAULT-NAME-003
def test_ambiguity_ignores_deeper_notes_with_the_same_name(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    outside(remote, clock, {"ideas/sub/Twin.md": "# Twin deeper\n"})
    with pytest.raises(VaultError) as exc:
        service.read_note(OWNER, "Twin")
    assert exc.value.code == "ambiguous"
    assert "ideas/sub/Twin.md" not in strings_in(exc.value.detail)


# @spec VAULT-NAME-003
def test_ambiguous_name_fails_a_changing_call_and_changes_nothing(
    service: VaultService, remote: Remote
) -> None:
    head = remote.head()
    with pytest.raises(VaultError) as exc:
        service.append_to_note(OWNER, "Twin", "more\n")
    assert exc.value.code == "ambiguous"
    assert remote.head() == head


# --- VAULT-NAME-004: not found ---------------------------------------------------------------


# @spec VAULT-NAME-004
def test_unknown_name_is_not_found(service: VaultService) -> None:
    with pytest.raises(VaultError) as exc:
        service.read_note(OWNER, "Nonexistent")
    assert exc.value.code == "not_found"


# @spec VAULT-NAME-004
def test_unknown_path_is_not_found(service: VaultService) -> None:
    with pytest.raises(VaultError) as exc:
        service.read_note(OWNER, "ideas/Nope.md")
    assert exc.value.code == "not_found"


# @spec VAULT-NAME-004
def test_path_into_a_dot_directory_is_not_found(service: VaultService) -> None:
    with pytest.raises(VaultError) as exc:
        service.read_note(OWNER, ".hidden/Secret.md")
    assert exc.value.code == "not_found"


# @spec VAULT-NAME-004
def test_name_of_a_note_only_in_a_dot_directory_is_not_found(service: VaultService) -> None:
    with pytest.raises(VaultError) as exc:
        service.read_note(OWNER, "Secret")
    assert exc.value.code == "not_found"


# @spec VAULT-NAME-004
def test_unknown_note_fails_a_changing_call_and_changes_nothing(
    service: VaultService, remote: Remote
) -> None:
    head = remote.head()
    with pytest.raises(VaultError) as exc:
        service.append_to_note(OWNER, "Nonexistent", "more\n")
    assert exc.value.code == "not_found"
    assert remote.head() == head


# --- VAULT-NAME-005: aliases do not address ---------------------------------------------------


# @spec VAULT-NAME-005
def test_alias_does_not_resolve_to_its_note(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    outside(remote, clock, {"notes/Aliased.md": "---\naliases: [Zed, Zulu]\n---\n# Aliased\n"})
    with pytest.raises(VaultError) as exc:
        service.read_note(OWNER, "Zed")
    assert exc.value.code == "not_found"


# @spec VAULT-NAME-005
def test_alias_does_not_compete_with_a_name(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    """A shallower note aliased `Alpha` neither wins nor makes the name ambiguous."""
    outside(remote, clock, {"Aliased.md": "---\naliases: [Alpha]\n---\n# Aliased\n"})
    result = service.read_note(OWNER, "Alpha")
    assert result["path"] == "ideas/Alpha.md"


# --- VAULT-NAME-006: equivalent destination paths ---------------------------------------------


# @spec VAULT-NAME-006
def test_create_at_a_case_variant_of_an_existing_path_is_confinement(
    service: VaultService, remote: Remote
) -> None:
    head = remote.head()
    with pytest.raises(VaultError) as exc:
        service.create_note(OWNER, "ideas/alpha.md", "# imposter\n")
    assert exc.value.code == "confinement"
    assert remote.head() == head
    assert "ideas/alpha.md" not in remote.files_lines()


# @spec VAULT-NAME-006
def test_create_at_a_case_variant_folder_of_an_existing_path_is_confinement(
    service: VaultService, remote: Remote
) -> None:
    head = remote.head()
    with pytest.raises(VaultError) as exc:
        service.create_note(OWNER, "IDEAS/Alpha.md", "# imposter\n")
    assert exc.value.code == "confinement"
    assert remote.head() == head


# @spec VAULT-NAME-006
def test_create_at_an_nfd_variant_of_an_nfc_path_is_confinement(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    outside(remote, clock, {f"notes/{CAFE_NFC}.md": "# Café\n"})
    head = remote.head()
    with pytest.raises(VaultError) as exc:
        service.create_note(OWNER, f"notes/{CAFE_NFD}.md", "# twin\n")
    assert exc.value.code == "confinement"
    assert remote.head() == head
    assert tree(remote).count(f"notes/{CAFE_NFC}.md") == 1
    assert f"notes/{CAFE_NFD}.md" not in tree(remote)


# @spec VAULT-NAME-006
def test_move_onto_a_case_variant_of_an_existing_path_is_confinement(
    service: VaultService, remote: Remote
) -> None:
    head = remote.head()
    with pytest.raises(VaultError) as exc:
        service.move_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), "ideas/ALPHA.md")
    assert exc.value.code == "confinement"
    assert remote.head() == head
    assert remote.show("ideas/Beta.md") is not None
    assert remote.show("ideas/Alpha.md") is not None


# @spec VAULT-NAME-006
def test_move_onto_an_equivalent_path_in_another_folder_case_is_confinement(
    service: VaultService, remote: Remote
) -> None:
    head = remote.head()
    with pytest.raises(VaultError) as exc:
        service.move_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), "Notes/gamma.md")
    assert exc.value.code == "confinement"
    assert remote.head() == head


# @spec VAULT-NAME-006
def test_create_with_the_name_of_a_note_elsewhere_is_allowed(
    service: VaultService, remote: Remote
) -> None:
    """Only an equivalent *path* conflicts; sharing a name with a note in another folder does
    not."""
    result = service.create_note(OWNER, "notes/Alpha.md", "# Second alpha\n")
    assert result["path"] == "notes/Alpha.md"
    assert remote.show("notes/Alpha.md") == "# Second alpha\n"
    assert remote.show("ideas/Alpha.md") is not None


# --- VAULT-NAME-007: names a link cannot reach -----------------------------------------------

BAD_NAMES = [
    pytest.param("ideas/a[b.md", id="open-bracket"),
    pytest.param("ideas/a]b.md", id="close-bracket"),
    pytest.param("ideas/a#b.md", id="hash"),
    pytest.param("ideas/a^b.md", id="caret"),
    pytest.param("ideas/a|b.md", id="pipe"),
    pytest.param("ideas/a:b.md", id="colon"),
    pytest.param("ideas/ leading.md", id="leading-space"),
    pytest.param("ideas/trailing .md", id="trailing-space"),
    pytest.param("ideas/\ttab.md", id="leading-tab"),
]


# @spec VAULT-NAME-007
@pytest.mark.parametrize("path", BAD_NAMES)
def test_create_with_an_unlinkable_name_is_confinement(
    service: VaultService, remote: Remote, path: str
) -> None:
    head = remote.head()
    with pytest.raises(VaultError) as exc:
        service.create_note(OWNER, path, "# bad\n")
    assert exc.value.code == "confinement"
    assert remote.head() == head


# @spec VAULT-NAME-007
@pytest.mark.parametrize("path", BAD_NAMES)
def test_move_to_an_unlinkable_name_is_confinement(
    service: VaultService, remote: Remote, path: str
) -> None:
    head = remote.head()
    with pytest.raises(VaultError) as exc:
        service.move_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), path)
    assert exc.value.code == "confinement"
    assert remote.head() == head
    assert remote.show("ideas/Beta.md") is not None


# @spec VAULT-NAME-007
def test_name_with_inner_spaces_is_allowed(service: VaultService, remote: Remote) -> None:
    result = service.create_note(OWNER, "ideas/a name with spaces.md", "# ok\n")
    assert result["path"] == "ideas/a name with spaces.md"
    assert remote.show("ideas/a name with spaces.md") == "# ok\n"
