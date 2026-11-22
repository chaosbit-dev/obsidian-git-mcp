"""Failure codes, their precedence, and failures never passing as empty results (VAULT-ERR-*)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from obsidian_git_mcp.errors import CODES, VaultError
from tests.conftest import GUARD_WORD, OWNER, READER, git

if TYPE_CHECKING:
    from collections.abc import Callable

    from obsidian_git_mcp.service import VaultService
    from tests.conftest import Clock, FakeRefusals, Remote

STALE = "0" * 40
PAST_FRESHNESS = 60.0
TOO_BIG = "x" * 1_100_000  # over the default 1 MB cap
SEED_GAMMA_EDITED = "---\nstatus: reference\n---\n# Gamma\n\nEdited on the Mac.\n"


def blob(remote: Remote, path: str) -> str:
    return git(remote.bare, "rev-parse", f"{remote.branch}:{path}").strip()


def failure(call: Callable[[], object]) -> VaultError:
    with pytest.raises(VaultError) as info:
        call()
    return info.value


def mentions(detail: dict[str, Any], *needles: str) -> bool:
    text = json.dumps(detail, default=str)
    return all(n in text for n in needles)


def break_remote(remote: Remote) -> None:
    """Make every fetch and push fail, as a deleted repository or revoked App would."""
    remote.bare.rename(remote.bare.with_name("gone.git"))


# --- VAULT-ERR-001: exactly one listed code, with what it concerns -------------------------


# @spec VAULT-ERR-001, VAULT-ERR-002
def test_codes_are_exactly_the_specified_list() -> None:
    assert CODES == (
        "confinement",
        "not_found",
        "ambiguous",
        "stale_revision",
        "frontmatter_unparsed",
        "refused_pinned",
        "refused_guard",
        "refusal_source_unavailable",
        "too_large",
        "fetch_failed",
        "push_conflict",
    )


# @spec VAULT-ERR-001
def test_not_found_names_the_note(service: VaultService) -> None:
    err = failure(lambda: service.read_note(OWNER, "Nowhere"))
    assert err.code == "not_found"
    assert mentions(err.detail, "Nowhere")


# @spec VAULT-ERR-001
def test_ambiguous_lists_the_candidates(service: VaultService) -> None:
    err = failure(lambda: service.read_note(OWNER, "Twin"))
    assert err.code == "ambiguous"
    assert mentions(err.detail, "ideas/Twin.md", "notes/Twin.md")


# @spec VAULT-ERR-001
def test_stale_revision_carries_the_current_revision(
    service: VaultService, remote: Remote
) -> None:
    err = failure(lambda: service.edit_note(OWNER, "ideas/Beta.md", STALE, body="new\n"))
    assert err.code == "stale_revision"
    assert mentions(err.detail, blob(remote, "ideas/Beta.md"))


# @spec VAULT-ERR-001
def test_confinement_names_the_path(service: VaultService) -> None:
    err = failure(lambda: service.create_note(OWNER, "../escape.md", "x\n"))
    assert err.code == "confinement"
    assert mentions(err.detail, "../escape.md")


# @spec VAULT-ERR-001
def test_refused_pinned_names_the_note(service: VaultService, remote: Remote) -> None:
    rev = blob(remote, "ideas/Pinned.md")
    err = failure(lambda: service.delete_note(OWNER, "ideas/Pinned.md", rev))
    assert err.code == "refused_pinned"
    assert mentions(err.detail, "ideas/Pinned.md")


# @spec VAULT-ERR-001
def test_frontmatter_unparsed_names_the_note(service: VaultService, remote: Remote) -> None:
    rev = blob(remote, "ideas/Broken.md")
    err = failure(lambda: service.edit_note(OWNER, "ideas/Broken.md", rev, fields={"status": "x"}))
    assert err.code == "frontmatter_unparsed"
    assert mentions(err.detail, "ideas/Broken.md")


# @spec VAULT-ERR-001
def test_fetch_failed_is_its_own_code(service: VaultService, remote: Remote) -> None:
    break_remote(remote)
    err = failure(lambda: service.append_to_note(OWNER, "ideas/Beta.md", "more\n"))
    assert err.code == "fetch_failed"
    assert err.detail


# @spec VAULT-ERR-001
def test_every_failure_raises_a_single_listed_code(service: VaultService) -> None:
    err = failure(lambda: service.read_note(READER, "Nowhere"))
    assert err.code in CODES
    assert isinstance(err.code, str)


# --- VAULT-ERR-002: the first applicable code in list order wins ---------------------------


# @spec VAULT-ERR-002
def test_confinement_before_not_found(service: VaultService) -> None:
    err = failure(lambda: service.move_note(OWNER, "Nowhere", STALE, "../escape.md"))
    assert err.code == "confinement"


# @spec VAULT-ERR-002
def test_not_found_before_stale_revision(service: VaultService) -> None:
    err = failure(lambda: service.move_note(OWNER, "Nowhere", STALE, "ideas/New.md"))
    assert err.code == "not_found"


# @spec VAULT-ERR-002
def test_ambiguous_before_stale_revision(service: VaultService) -> None:
    err = failure(lambda: service.edit_note(OWNER, "Twin", STALE, body="x\n"))
    assert err.code == "ambiguous"


# @spec VAULT-ERR-002
def test_stale_revision_before_frontmatter_unparsed(service: VaultService) -> None:
    err = failure(
        lambda: service.edit_note(OWNER, "ideas/Broken.md", STALE, fields={"status": "x"})
    )
    assert err.code == "stale_revision"


# @spec VAULT-ERR-002
def test_stale_revision_before_refused_pinned(service: VaultService) -> None:
    err = failure(lambda: service.move_note(OWNER, "ideas/Pinned.md", STALE, "ideas/P.md"))
    assert err.code == "stale_revision"


# @spec VAULT-ERR-002
def test_frontmatter_unparsed_before_refused_guard(service: VaultService, remote: Remote) -> None:
    rev = blob(remote, "ideas/Broken.md")
    err = failure(
        lambda: service.edit_note(OWNER, "ideas/Broken.md", rev, fields={"status": GUARD_WORD})
    )
    assert err.code == "frontmatter_unparsed"


# @spec VAULT-ERR-002
def test_refused_pinned_before_refused_guard(service: VaultService, remote: Remote) -> None:
    # The destination name itself would add a guard match.
    rev = blob(remote, "ideas/Pinned.md")
    err = failure(
        lambda: service.move_note(OWNER, "ideas/Pinned.md", rev, f"ideas/{GUARD_WORD}.md")
    )
    assert err.code == "refused_pinned"


# @spec VAULT-ERR-002
def test_refused_guard_before_refusal_source_unavailable(
    service: VaultService, remote: Remote, refusals: FakeRefusals
) -> None:
    refusals.unreadable.add("superseded.yaml")  # guards stay readable
    rev = blob(remote, "ideas/Beta.md")
    err = failure(lambda: service.edit_note(OWNER, "ideas/Beta.md", rev, body=f"{GUARD_WORD}\n"))
    assert err.code == "refused_guard"


# @spec VAULT-ERR-002
def test_refused_pinned_before_refusal_source_unavailable(
    service: VaultService, remote: Remote, refusals: FakeRefusals
) -> None:
    refusals.unreadable.add("guards.yaml")  # pins stay readable
    rev = blob(remote, "ideas/Pinned.md")
    err = failure(lambda: service.delete_note(OWNER, "ideas/Pinned.md", rev))
    assert err.code == "refused_pinned"


# @spec VAULT-ERR-002
def test_refusal_source_unavailable_before_too_large(
    service: VaultService, remote: Remote, refusals: FakeRefusals
) -> None:
    refusals.unreadable.add("guards.yaml")
    rev = blob(remote, "ideas/Beta.md")
    err = failure(lambda: service.edit_note(OWNER, "ideas/Beta.md", rev, body=TOO_BIG))
    assert err.code == "refusal_source_unavailable"


# @spec VAULT-ERR-002
def test_confinement_before_fetch_failed(service: VaultService, remote: Remote) -> None:
    """A path that can never be valid is refused as such, even when the remote is down."""
    break_remote(remote)
    err = failure(lambda: service.create_note(OWNER, "/abs/path.md", "x\n"))
    assert err.code == "confinement"


# @spec VAULT-ERR-002
def test_too_large_before_fetch_failed(service: VaultService, remote: Remote) -> None:
    """A body over the cap is too large whatever the remote holds."""
    break_remote(remote)
    err = failure(lambda: service.create_note(OWNER, "ideas/Huge.md", TOO_BIG))
    assert err.code == "too_large"


# --- VAULT-ERR-003: revision checked before the no-op decision ------------------------------


# @spec VAULT-ERR-003
def test_stale_revision_fails_an_edit_that_would_change_nothing(
    service: VaultService, remote: Remote
) -> None:
    before = remote.commits()
    # Alpha's status is already "idea": with a current revision this is a no-op.
    err = failure(
        lambda: service.edit_note(OWNER, "ideas/Alpha.md", STALE, fields={"status": "idea"})
    )
    assert err.code == "stale_revision"
    assert mentions(err.detail, blob(remote, "ideas/Alpha.md"))
    assert remote.commits() == before


# @spec VAULT-ERR-003
def test_stale_revision_fails_a_move_onto_its_own_path(
    service: VaultService, remote: Remote
) -> None:
    before = remote.commits()
    err = failure(lambda: service.move_note(OWNER, "ideas/Beta.md", STALE, "ideas/Beta.md"))
    assert err.code == "stale_revision"
    assert remote.commits() == before


# @spec VAULT-ERR-003
def test_revision_made_stale_by_the_mac_fails_a_no_op_edit(
    service: VaultService, remote: Remote
) -> None:
    old = blob(remote, "notes/Gamma.md")
    remote.mac_commit({"notes/Gamma.md": SEED_GAMMA_EDITED})
    before = remote.commits()
    err = failure(
        lambda: service.edit_note(OWNER, "notes/Gamma.md", old, fields={"status": "reference"})
    )
    assert err.code == "stale_revision"
    assert mentions(err.detail, blob(remote, "notes/Gamma.md"))
    assert remote.commits() == before


# --- VAULT-ERR-004: a failure is never an empty result --------------------------------------


# @spec VAULT-ERR-004
def test_search_fetch_failure_is_not_an_empty_result(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    break_remote(remote)
    clock.advance(PAST_FRESHNESS)
    err = failure(lambda: service.search_notes(OWNER, query="beta"))
    assert err.code == "fetch_failed"


# @spec VAULT-ERR-004
def test_list_fetch_failure_is_not_an_empty_result(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    break_remote(remote)
    clock.advance(PAST_FRESHNESS)
    err = failure(lambda: service.list_notes(OWNER, folder="ideas"))
    assert err.code == "fetch_failed"


# @spec VAULT-ERR-004
def test_read_fetch_failure_is_not_an_empty_note(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    break_remote(remote)
    clock.advance(PAST_FRESHNESS)
    err = failure(lambda: service.read_note(OWNER, "ideas/Beta.md"))
    assert err.code == "fetch_failed"


# @spec VAULT-ERR-004
def test_a_genuinely_empty_search_is_a_result_not_a_failure(service: VaultService) -> None:
    """The converse: no match is an empty page with a total, never an error."""
    result = service.search_notes(OWNER, query="no-note-contains-this-zzq")
    assert result["notes"] == []
    assert result["total"] == 0
    assert result["commit"]
