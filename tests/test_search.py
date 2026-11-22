"""search_notes and list_notes (VAULT-SEARCH-001..008), against the seeded vault.

Resolvable notes in SEED (13): README.md, ideas/README.md, notes/README.md, ideas/Alpha.md,
ideas/Beta.md, notes/Gamma.md, ideas/Twin.md, notes/Twin.md, Deep.md, ideas/sub/Deep.md,
ideas/Broken.md, notes/Legacy.md, ideas/Pinned.md. `.hidden/Secret.md` is not resolvable.

The service fixture clones at start and the test clock never moves on its own, so a test that
changes the remote from the Mac advances the clock past the freshness window before reading.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from obsidian_git_mcp.service import VaultService
from tests.conftest import OWNER, READER, SEED, git, make_settings

if TYPE_CHECKING:
    from pathlib import Path

    from tests.conftest import Clock, FakeRefusals, FakeTokens, Remote

RESOLVABLE = sorted(p for p in SEED if not p.startswith("."))
PAST_FRESHNESS = 60.0


def paths(result: dict[str, Any]) -> list[str]:
    return [n["path"] for n in result["notes"]]


def blob(remote: Remote, path: str) -> str:
    return git(remote.bare, "rev-parse", f"{remote.branch}:{path}").strip()


def mac_commit_at(remote: Remote, files: dict[str, str], when: str, message: str) -> None:
    """Commit from the Mac with a fixed author and committer date, then push."""
    git(remote.mac, "pull", "--rebase", "-q")
    for path, text in files.items():
        p = remote.mac / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        git(remote.mac, "add", path)
    env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    git(remote.mac, "commit", "-q", "-m", message, env=env)
    git(remote.mac, "push", "-q", "origin", remote.branch)


def mac_move_at(remote: Remote, src: str, dst: str, when: str) -> None:
    git(remote.mac, "pull", "--rebase", "-q")
    git(remote.mac, "mv", src, dst)
    env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    git(remote.mac, "commit", "-q", "-m", f"move {src}", env=env)
    git(remote.mac, "push", "-q", "origin", remote.branch)


# --- VAULT-SEARCH-001: every term, as a case-folded substring of name, frontmatter or body ----


# @spec VAULT-SEARCH-001
def test_search_matches_body_case_insensitively(service: VaultService) -> None:
    result = service.search_notes(OWNER, query="DESIGN TEXT")
    assert paths(result) == ["ideas/Beta.md"]


# @spec VAULT-SEARCH-001
def test_search_requires_every_term(service: VaultService) -> None:
    assert paths(service.search_notes(OWNER, query="twin")) == ["ideas/Twin.md", "notes/Twin.md"]
    assert paths(service.search_notes(OWNER, query="twin notes")) == ["notes/Twin.md"]


# @spec VAULT-SEARCH-001
def test_search_matches_substrings_not_words(service: VaultService) -> None:
    # "lph" is inside Alpha's name and body, and inside Gamma's links to Alpha.
    assert paths(service.search_notes(OWNER, query="lph")) == ["ideas/Alpha.md", "notes/Gamma.md"]


# @spec VAULT-SEARCH-001
def test_search_matches_frontmatter_text(service: VaultService) -> None:
    # "P2" appears only as Alpha's priority value.
    assert paths(service.search_notes(OWNER, query="p2")) == ["ideas/Alpha.md"]


# @spec VAULT-SEARCH-001
def test_search_matches_name_alone(service: VaultService, remote: Remote, clock: Clock) -> None:
    remote.mac_commit({"ideas/Zebra Crossing.md": "nothing to see\n"})
    clock.advance(PAST_FRESHNESS)
    assert paths(service.search_notes(OWNER, query="zebra")) == ["ideas/Zebra Crossing.md"]


# @spec VAULT-SEARCH-001
def test_search_uses_case_folding_not_lowercasing(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    """casefold() maps "ß" to "ss"; lower() does not, so this separates the two."""
    remote.mac_commit({"notes/Street.md": "# Street\n\nHauptstraße 1\n"})
    clock.advance(PAST_FRESHNESS)
    assert paths(service.search_notes(OWNER, query="HAUPTSTRASSE")) == ["notes/Street.md"]


# @spec VAULT-SEARCH-001
def test_search_excludes_dot_directories(service: VaultService) -> None:
    result = service.search_notes(OWNER, query="secret")
    assert result["notes"] == []
    assert result["total"] == 0


# @spec VAULT-SEARCH-001
def test_search_excludes_trash_by_default(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    remote.mac_commit({"trash/ideas/Old.md": "# Old\n\nquuxword\n"})
    clock.advance(PAST_FRESHNESS)
    assert paths(service.search_notes(OWNER, query="quuxword")) == []


# @spec VAULT-SEARCH-001
def test_search_ignores_non_markdown_files(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    remote.mac_commit({"ideas/data.txt": "quuxword\n", "ideas/Real.MD": "quuxword\n"})
    clock.advance(PAST_FRESHNESS)
    assert paths(service.search_notes(OWNER, query="quuxword")) == ["ideas/Real.MD"]


# @spec VAULT-SEARCH-001
def test_read_mode_caller_can_search(service: VaultService) -> None:
    assert paths(service.search_notes(READER, query="DESIGN TEXT")) == ["ideas/Beta.md"]


# --- VAULT-SEARCH-002: folder ---------------------------------------------------------------


# @spec VAULT-SEARCH-002
def test_search_folder_narrows(service: VaultService) -> None:
    assert paths(service.search_notes(OWNER, query="twin", folder="notes")) == ["notes/Twin.md"]


# @spec VAULT-SEARCH-002
def test_list_folder_includes_subfolders(service: VaultService) -> None:
    result = service.list_notes(OWNER, folder="ideas")
    assert sorted(paths(result)) == [
        "ideas/Alpha.md",
        "ideas/Beta.md",
        "ideas/Broken.md",
        "ideas/Pinned.md",
        "ideas/README.md",
        "ideas/Twin.md",
        "ideas/sub/Deep.md",
    ]


# @spec VAULT-SEARCH-002
def test_folder_is_a_path_prefix_not_a_string_prefix(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    remote.mac_commit({"notes-old/Stale.md": "# Stale\n"})
    clock.advance(PAST_FRESHNESS)
    listed = paths(service.list_notes(OWNER, folder="notes"))
    assert "notes-old/Stale.md" not in listed
    assert sorted(listed) == [
        "notes/Gamma.md",
        "notes/Legacy.md",
        "notes/README.md",
        "notes/Twin.md",
    ]


# @spec VAULT-SEARCH-002
def test_list_excludes_trash_without_folder(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    remote.mac_commit({"trash/ideas/Old.md": "# Old\n"})
    clock.advance(PAST_FRESHNESS)
    assert sorted(paths(service.list_notes(OWNER))) == RESOLVABLE


# @spec VAULT-SEARCH-002
def test_list_includes_trash_when_folder_names_it(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    remote.mac_commit({"trash/ideas/Old.md": "# Old\n"})
    clock.advance(PAST_FRESHNESS)
    assert paths(service.list_notes(OWNER, folder="trash")) == ["trash/ideas/Old.md"]


# @spec VAULT-SEARCH-002
def test_search_includes_trash_when_folder_names_it(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    remote.mac_commit({"trash/ideas/Old.md": "# Old\n\nquuxword\n"})
    clock.advance(PAST_FRESHNESS)
    result = service.search_notes(OWNER, query="quuxword", folder="trash")
    assert paths(result) == ["trash/ideas/Old.md"]


# @spec VAULT-SEARCH-002
def test_configured_trash_folder_is_the_one_excluded(
    remote: Remote, tmp_path: Path, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> None:
    remote.mac_commit({"bin/Gone.md": "# Gone\n", "trash/Kept.md": "# Kept\n"})
    settings = make_settings(remote, tmp_path / "work2", trash_folder="bin")
    svc = VaultService(settings, tokens, refusals, clock=clock)
    svc.start()
    listed = paths(svc.list_notes(OWNER))
    assert "bin/Gone.md" not in listed
    assert "trash/Kept.md" in listed


# --- VAULT-SEARCH-003: fields ---------------------------------------------------------------


# @spec VAULT-SEARCH-003
def test_list_fields_scalar_equality(service: VaultService) -> None:
    assert paths(service.list_notes(OWNER, fields={"status": "idea"})) == ["ideas/Alpha.md"]


# @spec VAULT-SEARCH-003
def test_list_fields_list_value_contains(service: VaultService) -> None:
    assert paths(service.list_notes(OWNER, fields={"area": "HA"})) == ["ideas/Alpha.md"]
    assert paths(service.list_notes(OWNER, fields={"tags": "y"})) == ["notes/Gamma.md"]


# @spec VAULT-SEARCH-003
def test_list_fields_list_element_must_equal_not_contain(service: VaultService) -> None:
    # Alpha's area holds "Gonk/K3s"; "Gonk" is a substring of an element, not an element.
    result = service.list_notes(OWNER, fields={"area": "Gonk"})
    assert result["notes"] == []
    assert result["total"] == 0


# @spec VAULT-SEARCH-003
def test_list_fields_compares_as_text(service: VaultService) -> None:
    # Gamma has `n: 1`, a YAML integer.
    assert paths(service.list_notes(OWNER, fields={"n": "1"})) == ["notes/Gamma.md"]
    assert paths(service.list_notes(OWNER, fields={"n": 1})) == ["notes/Gamma.md"]


# @spec VAULT-SEARCH-003
def test_list_fields_scalar_does_not_match_substring(service: VaultService) -> None:
    assert service.list_notes(OWNER, fields={"status": "ide"})["notes"] == []


# @spec VAULT-SEARCH-003
def test_list_fields_every_key_must_match(service: VaultService) -> None:
    both = service.list_notes(OWNER, fields={"status": "reference", "tags": "x"})
    assert paths(both) == ["notes/Gamma.md"]
    mixed = service.list_notes(OWNER, fields={"status": "idea", "tags": "x"})
    assert mixed["notes"] == []


# @spec VAULT-SEARCH-003
def test_search_fields_narrow_query_results(service: VaultService) -> None:
    # "beta" matches Alpha, Beta and Gamma; only Alpha has status idea.
    result = service.search_notes(OWNER, query="beta", fields={"status": "idea"})
    assert paths(result) == ["ideas/Alpha.md"]


# --- VAULT-SEARCH-004: result shape ---------------------------------------------------------


# @spec VAULT-SEARCH-004
def test_search_result_carries_path_name_frontmatter_revision(
    service: VaultService, remote: Remote
) -> None:
    (note,) = service.search_notes(OWNER, query="p2")["notes"]
    assert note["path"] == "ideas/Alpha.md"
    assert note["name"] == "Alpha"
    assert note["revision"] == blob(remote, "ideas/Alpha.md")
    assert note["frontmatter"] == {
        "status": "idea",
        "area": ["HA", "Gonk/K3s"],
        "priority": "P2",
        "related": "[[Gamma]]",
    }


# @spec VAULT-SEARCH-004
def test_search_returns_at_most_three_lines_each_containing_a_term(
    service: VaultService,
) -> None:
    # Alpha has more than three lines containing "beta".
    (note,) = [
        n
        for n in service.search_notes(OWNER, query="beta")["notes"]
        if n["path"] == "ideas/Alpha.md"
    ]
    lines = note["lines"]
    assert 1 <= len(lines) <= 3
    alpha_lines = SEED["ideas/Alpha.md"].splitlines()
    for line in lines:
        assert "beta" in line.casefold()
        assert line in alpha_lines


# @spec VAULT-SEARCH-004
def test_search_lines_contain_any_query_term(service: VaultService) -> None:
    (note,) = service.search_notes(OWNER, query="design text")["notes"]
    assert note["lines"]
    for line in note["lines"]:
        folded = line.casefold()
        assert "design" in folded or "text" in folded
        assert line in SEED["ideas/Beta.md"].splitlines()


# @spec VAULT-SEARCH-004
def test_search_lines_empty_when_only_the_name_matches(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    remote.mac_commit({"ideas/Zebra Crossing.md": "nothing to see\n"})
    clock.advance(PAST_FRESHNESS)
    (note,) = service.search_notes(OWNER, query="zebra")["notes"]
    assert note["name"] == "Zebra Crossing"
    assert note["lines"] == []


# --- VAULT-SEARCH-005: totals ---------------------------------------------------------------


# @spec VAULT-SEARCH-005
def test_search_total_is_carried_on_every_page(service: VaultService) -> None:
    # "beta" matches exactly ideas/Alpha.md, ideas/Beta.md and notes/Gamma.md.
    pages = [service.search_notes(OWNER, query="beta", limit=1, page=p) for p in (1, 2, 3, 4)]
    assert [r["total"] for r in pages] == [3, 3, 3, 3]
    assert [len(r["notes"]) for r in pages] == [1, 1, 1, 0]


# @spec VAULT-SEARCH-005
def test_list_total_counts_all_matches_not_the_page(service: VaultService) -> None:
    result = service.list_notes(OWNER, limit=2)
    assert result["total"] == len(RESOLVABLE)
    assert len(result["notes"]) == 2


# @spec VAULT-SEARCH-005
def test_list_total_reflects_filters(service: VaultService) -> None:
    result = service.list_notes(OWNER, folder="notes", limit=1)
    assert result["total"] == 4
    assert len(result["notes"]) == 1


# @spec VAULT-SEARCH-005
def test_every_page_carries_the_commit_it_was_answered_from(
    service: VaultService, remote: Remote
) -> None:
    head = remote.head()
    assert service.search_notes(OWNER, query="beta", limit=1, page=2)["commit"] == head
    assert service.list_notes(OWNER, limit=1, page=3)["commit"] == head


# --- VAULT-SEARCH-006: search order ---------------------------------------------------------


# @spec VAULT-SEARCH-006
def test_search_orders_by_path_among_equal_term_counts(service: VaultService) -> None:
    assert paths(service.search_notes(OWNER, query="beta")) == [
        "ideas/Alpha.md",
        "ideas/Beta.md",
        "notes/Gamma.md",
    ]
    assert paths(service.search_notes(OWNER, query="deep")) == ["Deep.md", "ideas/sub/Deep.md"]


# @spec VAULT-SEARCH-006
def test_search_pages_follow_the_stated_order(service: VaultService) -> None:
    first = paths(service.search_notes(OWNER, query="beta", limit=2, page=1))
    second = paths(service.search_notes(OWNER, query="beta", limit=2, page=2))
    assert first + second == ["ideas/Alpha.md", "ideas/Beta.md", "notes/Gamma.md"]


# --- VAULT-SEARCH-007: list order -----------------------------------------------------------


# @spec VAULT-SEARCH-007
def test_list_orders_by_path_when_commit_times_tie(service: VaultService) -> None:
    # Every seeded note was added by the one seed commit.
    assert paths(service.list_notes(OWNER)) == RESOLVABLE


# @spec VAULT-SEARCH-007
def test_list_orders_newest_change_first(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    mac_commit_at(remote, {"notes/Gamma.md": "# Gamma\n\nedited\n"}, "2030-01-01T00:00:00Z", "g")
    mac_commit_at(remote, {"ideas/Beta.md": "# Beta\n\nedited\n"}, "2030-01-02T00:00:00Z", "b")
    clock.advance(PAST_FRESHNESS)
    listed = paths(service.list_notes(OWNER))
    assert listed[:2] == ["ideas/Beta.md", "notes/Gamma.md"]
    assert listed[2:] == [p for p in RESOLVABLE if p not in {"ideas/Beta.md", "notes/Gamma.md"}]


# @spec VAULT-SEARCH-007
def test_list_order_uses_commit_time_not_topology(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    """A later commit carrying an older date sorts by its date."""
    mac_commit_at(remote, {"ideas/Beta.md": "# Beta\n\nnew\n"}, "2030-01-05T00:00:00Z", "b")
    mac_commit_at(remote, {"notes/Gamma.md": "# Gamma\n\nnew\n"}, "2030-01-02T00:00:00Z", "g")
    clock.advance(PAST_FRESHNESS)
    assert paths(service.list_notes(OWNER))[:2] == ["ideas/Beta.md", "notes/Gamma.md"]


# @spec VAULT-SEARCH-007
def test_list_order_counts_the_commit_that_created_the_current_path(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    mac_commit_at(remote, {"notes/Gamma.md": "# Gamma\n\nnew\n"}, "2030-01-01T00:00:00Z", "g")
    mac_move_at(remote, "notes/Twin.md", "notes/Twin Moved.md", "2030-01-03T00:00:00Z")
    clock.advance(PAST_FRESHNESS)
    assert paths(service.list_notes(OWNER))[:2] == ["notes/Twin Moved.md", "notes/Gamma.md"]


# @spec VAULT-SEARCH-007
def test_list_order_applies_across_pages(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    mac_commit_at(remote, {"notes/Gamma.md": "# Gamma\n\nnew\n"}, "2030-01-01T00:00:00Z", "g")
    clock.advance(PAST_FRESHNESS)
    first = paths(service.list_notes(OWNER, limit=5, page=1))
    rest = paths(service.list_notes(OWNER, limit=500, page=1))
    assert first == rest[:5]
    assert first[0] == "notes/Gamma.md"


# --- VAULT-SEARCH-008: limit caps -----------------------------------------------------------


# @spec VAULT-SEARCH-008
def test_search_limit_is_capped_at_100(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    remote.mac_commit({f"bulk/Cap {i:03}.md": "capword\n" for i in range(105)})
    clock.advance(PAST_FRESHNESS)
    result = service.search_notes(OWNER, query="capword", limit=1000)
    assert len(result["notes"]) == 100
    assert result["total"] == 105


# @spec VAULT-SEARCH-008
def test_search_cap_applies_per_page(service: VaultService, remote: Remote, clock: Clock) -> None:
    remote.mac_commit({f"bulk/Cap {i:03}.md": "capword\n" for i in range(105)})
    clock.advance(PAST_FRESHNESS)
    second = service.search_notes(OWNER, query="capword", limit=1000, page=2)
    assert paths(second) == [f"bulk/Cap {i:03}.md" for i in range(100, 105)]


# @spec VAULT-SEARCH-008
def test_list_limit_is_capped_at_500(service: VaultService, remote: Remote, clock: Clock) -> None:
    remote.mac_commit({f"bulk/Item {i:03}.md": "x\n" for i in range(495)})
    clock.advance(PAST_FRESHNESS)
    result = service.list_notes(OWNER, limit=10_000)
    assert len(result["notes"]) == 500
    assert result["total"] == 495 + len(RESOLVABLE)
