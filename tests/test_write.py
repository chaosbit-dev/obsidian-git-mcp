"""Write rules (VAULT-WRITE-*) and commit authorship (VAULT-ID-003), against a real bare remote."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from obsidian_git_mcp.config import Identity, Settings
from obsidian_git_mcp.errors import VaultError
from obsidian_git_mcp.service import VaultService
from tests.conftest import (
    OWNER,
    SEED,
    Clock,
    FakeRefusals,
    FakeTokens,
    Remote,
    git,
    make_settings,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# A second write identity whose Access email differs from its commit author email, so a test
# can tell the trailer (caller's Access email) apart from the author (identity's author email).
OTHER = Identity("other@example.com", "write", "Other Writer", "other.commits@example.com")


# --- helpers ----------------------------------------------------------------------------------


def blob(remote: Remote, path: str) -> str:
    """The git blob id of `path` at the remote branch tip: what the service calls `revision`."""
    return git(remote.bare, "rev-parse", f"{remote.branch}:{path}").strip()


def last_commit_paths(remote: Remote) -> set[str]:
    """Every path the remote tip commit touched (a rename shows both old and new paths)."""
    out = git(
        remote.bare,
        "diff-tree",
        "-r",
        "--no-commit-id",
        "--name-only",
        "--no-renames",
        remote.branch,
    )
    return set(out.splitlines())


def parent_of_head(remote: Remote) -> str:
    return git(remote.bare, "rev-parse", f"{remote.branch}^").strip()


def trailers(message: str) -> list[tuple[str, str]]:
    """Parse git trailers out of a commit message with git itself."""
    out = subprocess.run(
        ["git", "interpret-trailers", "--parse"],
        input=message,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pairs: list[tuple[str, str]] = []
    for line in out.splitlines():
        key, _, value = line.partition(":")
        pairs.append((key.strip(), value.strip()))
    return pairs


def build(
    remote: Remote,
    tmp_path: Path,
    *,
    tokens: FakeTokens | None = None,
    refusals: FakeRefusals | None = None,
    name: str = "work-custom",
    **overrides: Any,
) -> VaultService:
    settings: Settings = make_settings(remote, tmp_path / name, **overrides)
    svc = VaultService(settings, tokens or FakeTokens(), refusals or FakeRefusals(), clock=Clock())
    svc.start()
    return svc


def mac_race(remote: Remote, files: dict[str, str | None]) -> Callable[[], None]:
    """A race callback: the Mac commits `files` and pushes."""

    def race() -> None:
        remote.mac_commit(files)

    return race


@dataclass
class RacingTokens(FakeTokens):
    """Injects an outside commit between the service's fetch and its push.

    Technique: VAULT-ID-004 has the service mint a `contents: write` token for each push, through
    the public TokenSource port. That request necessarily comes after the call's fetch (the call
    must fetch first, VAULT-CLONE-004) and before the push it authenticates. Running a Mac commit
    from inside that request therefore moves the remote branch at exactly the point a real race
    would, without naming any private method or patching git. Each queued callback fires once, on
    the next write-token request; with two queued, both the first push and the retry's push are
    raced.
    """

    before_push: list[Callable[[], None]] = field(default_factory=list)

    def vault_token(self, permission: str) -> str:
        if permission == "write" and self.before_push:
            self.before_push.pop(0)()
        return super().vault_token(permission)


# --- VAULT-WRITE-001: one call, one commit --------------------------------------------------


# @spec VAULT-WRITE-001
def test_append_makes_exactly_one_commit(service: VaultService, remote: Remote) -> None:
    before = remote.commits()
    service.append_to_note(OWNER, "ideas/Beta.md", "Appended line.")
    assert remote.commits() == before + 1


# @spec VAULT-WRITE-001
def test_move_with_link_rewrites_is_one_commit(service: VaultService, remote: Remote) -> None:
    """A move that rewrites links in Alpha and Gamma still lands as a single commit."""
    before = remote.commits()
    service.move_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), "ideas/Beta2.md")
    assert remote.commits() == before + 1
    assert {"ideas/Beta.md", "ideas/Beta2.md", "ideas/Alpha.md", "notes/Gamma.md"} <= (
        last_commit_paths(remote)
    )


# @spec VAULT-WRITE-001
def test_commit_message_names_the_tool(service: VaultService, remote: Remote) -> None:
    service.create_note(OWNER, "ideas/Fresh.md", "# Fresh\n")
    subject = remote.last_commit()["message"].splitlines()[0]
    assert "create" in subject


# @spec VAULT-WRITE-001
def test_commit_message_names_every_changed_path(service: VaultService, remote: Remote) -> None:
    service.move_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), "ideas/Beta2.md")
    message = remote.last_commit()["message"]
    assert "move" in message.splitlines()[0]
    for path in last_commit_paths(remote):
        assert path in message, path


# @spec VAULT-WRITE-001
def test_commit_message_carries_trailer_with_callers_access_email(
    remote: Remote, tmp_path: Path
) -> None:
    svc = build(remote, tmp_path, identities=(OWNER, OTHER))
    svc.append_to_note(OTHER, "ideas/Beta.md", "From the other writer.")
    values = [value for _, value in trailers(remote.last_commit()["message"])]
    assert any(OTHER.email in value for value in values), values


# --- VAULT-WRITE-002: no-op writes ----------------------------------------------------------


# @spec VAULT-WRITE-002
def test_edit_with_identical_body_makes_no_commit(service: VaultService, remote: Remote) -> None:
    head = remote.head()
    result = service.edit_note(
        OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), body=SEED["ideas/Beta.md"]
    )
    assert result["changed"] is False
    assert remote.head() == head


# @spec VAULT-WRITE-002
def test_setting_field_to_its_current_value_makes_no_commit(
    service: VaultService, remote: Remote
) -> None:
    head = remote.head()
    result = service.edit_note(
        OWNER, "ideas/Alpha.md", blob(remote, "ideas/Alpha.md"), fields={"status": "idea"}
    )
    assert result["changed"] is False
    assert remote.head() == head
    assert remote.show("ideas/Alpha.md") == SEED["ideas/Alpha.md"]


# --- VAULT-WRITE-003: stale revisions -------------------------------------------------------


def _call_with_revision(svc: VaultService, tool: str, note: str, revision: str) -> Any:
    if tool == "edit_note":
        return svc.edit_note(OWNER, note, revision, body="# Replaced\n")
    if tool == "move_note":
        return svc.move_note(OWNER, note, revision, "notes/Moved.md")
    if tool == "delete_note":
        return svc.delete_note(OWNER, note, revision)
    return svc.archive_note(OWNER, note, revision, fields={"issue": "x#1"})


# @spec VAULT-WRITE-003
@pytest.mark.parametrize("tool", ["edit_note", "move_note", "delete_note", "archive_note"])
def test_stale_revision_is_refused_with_current_revision(
    service: VaultService, remote: Remote, tool: str
) -> None:
    old = blob(remote, "ideas/Twin.md")
    remote.mac_commit({"ideas/Twin.md": "# Twin in ideas\n\nEdited on the Mac.\n"})
    current = blob(remote, "ideas/Twin.md")
    head = remote.head()

    with pytest.raises(VaultError) as exc:
        _call_with_revision(service, tool, "ideas/Twin.md", old)

    assert exc.value.code == "stale_revision"
    assert current in exc.value.detail.values()
    assert remote.head() == head
    assert remote.show("ideas/Twin.md") == "# Twin in ideas\n\nEdited on the Mac.\n"


# --- VAULT-WRITE-004 / 005: the push race ---------------------------------------------------


# @spec VAULT-WRITE-004
def test_push_race_is_retried_on_new_tip_keeping_outside_commit(
    remote: Remote, tmp_path: Path
) -> None:
    tokens = RacingTokens()
    svc = build(remote, tmp_path, tokens=tokens)
    mac_text = "# Gamma\n\nRewritten on the Mac.\n"
    mac_heads: list[str] = []

    def race() -> None:
        mac_heads.append(remote.mac_commit({"notes/Gamma.md": mac_text}))

    tokens.before_push.append(race)
    before = remote.commits()

    result = svc.append_to_note(OWNER, "ideas/Beta.md", "Raced append.")

    assert result["changed"] is True
    assert remote.commits() == before + 2
    assert parent_of_head(remote) == mac_heads[0]
    assert result["commit"] == remote.head()
    assert remote.show("notes/Gamma.md") == mac_text
    beta = remote.show("ideas/Beta.md")
    assert beta is not None
    assert beta.count("Raced append.") == 1


# @spec VAULT-WRITE-004, VAULT-WRITE-003
def test_push_race_retry_rechecks_revision(remote: Remote, tmp_path: Path) -> None:
    tokens = RacingTokens()
    svc = build(remote, tmp_path, tokens=tokens)
    mac_text = "# Beta\n\nThe Mac got here first.\n"
    tokens.before_push.append(mac_race(remote, {"ideas/Beta.md": mac_text}))
    rev = blob(remote, "ideas/Beta.md")

    with pytest.raises(VaultError) as exc:
        svc.edit_note(OWNER, "ideas/Beta.md", rev, body="# Beta\n\nServer edit.\n")

    assert exc.value.code == "stale_revision"
    assert blob(remote, "ideas/Beta.md") in exc.value.detail.values()
    assert remote.show("ideas/Beta.md") == mac_text


# @spec VAULT-WRITE-004
def test_push_race_retry_reruns_resolution(remote: Remote, tmp_path: Path) -> None:
    """The Mac creates an equivalent path during the race; the retry must see it."""
    tokens = RacingTokens()
    svc = build(remote, tmp_path, tokens=tokens)
    tokens.before_push.append(mac_race(remote, {"ideas/fresh.md": "# fresh from mac\n"}))

    with pytest.raises(VaultError) as exc:
        svc.create_note(OWNER, "ideas/Fresh.md", "# Fresh from server\n")

    assert exc.value.code == "confinement"
    files = remote.files_lines()
    assert "ideas/fresh.md" in files
    assert "ideas/Fresh.md" not in files


# @spec VAULT-WRITE-004
def test_push_race_retry_rereads_refusals(remote: Remote, tmp_path: Path) -> None:
    """A pin added during the race is enforced by the retry (refusals are read per attempt)."""
    tokens = RacingTokens()
    refusals = FakeRefusals()
    svc = build(remote, tmp_path, tokens=tokens, refusals=refusals)

    def race() -> None:
        refusals.text["pins.txt"] += "ideas/Fresh.md\n"
        remote.mac_commit({"notes/Gamma.md": "# Gamma\n\nunrelated\n"})

    tokens.before_push.append(race)

    with pytest.raises(VaultError) as exc:
        svc.create_note(OWNER, "ideas/Fresh.md", "# Fresh\n")

    assert exc.value.code == "refused_pinned"
    assert remote.show("ideas/Fresh.md") is None


# @spec VAULT-WRITE-004
def test_push_race_retry_rewrites_links_added_by_outside_commit(
    remote: Remote, tmp_path: Path
) -> None:
    tokens = RacingTokens()
    svc = build(remote, tmp_path, tokens=tokens)
    tokens.before_push.append(mac_race(remote, {"notes/Late.md": "# Late\n\nSee [[Beta]].\n"}))

    result = svc.move_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), "ideas/Beta2.md")

    assert remote.show("notes/Late.md") == "# Late\n\nSee [[Beta2]].\n"
    assert "notes/Late.md" in result["also_changed"]
    assert remote.show("ideas/Beta.md") is None


# @spec VAULT-WRITE-005
def test_second_push_rejection_fails_with_push_conflict(remote: Remote, tmp_path: Path) -> None:
    tokens = RacingTokens()
    svc = build(remote, tmp_path, tokens=tokens)
    tokens.before_push.append(mac_race(remote, {"notes/Gamma.md": "# G1\n"}))
    tokens.before_push.append(mac_race(remote, {"notes/Gamma.md": "# G2\n"}))
    before = remote.commits()

    with pytest.raises(VaultError) as exc:
        svc.append_to_note(OWNER, "ideas/Beta.md", "Never lands.")

    assert exc.value.code == "push_conflict"
    assert remote.commits() == before + 2
    assert remote.last_commit()["author_name"] == "Mac Operator"
    assert remote.show("ideas/Beta.md") == SEED["ideas/Beta.md"]


# --- VAULT-WRITE-006: reset after failure ---------------------------------------------------


# @spec VAULT-WRITE-006
def test_clone_is_reset_after_push_conflict(remote: Remote, tmp_path: Path) -> None:
    """The next call's commit carries only its own change, not the failed call's leftovers."""
    tokens = RacingTokens()
    svc = build(remote, tmp_path, tokens=tokens)
    tokens.before_push.append(mac_race(remote, {"notes/Twin.md": "# T1\n"}))
    tokens.before_push.append(mac_race(remote, {"notes/Twin.md": "# T2\n"}))
    with pytest.raises(VaultError):
        svc.append_to_note(OWNER, "ideas/Beta.md", "Leftover from the failed call.")

    svc.append_to_note(OWNER, "notes/Gamma.md", "Next call.")

    assert last_commit_paths(remote) == {"notes/Gamma.md"}
    assert remote.show("ideas/Beta.md") == SEED["ideas/Beta.md"]
    assert remote.show("notes/Twin.md") == "# T2\n"


# @spec VAULT-WRITE-006
def test_clone_is_reset_after_stale_revision_on_retry(remote: Remote, tmp_path: Path) -> None:
    tokens = RacingTokens()
    svc = build(remote, tmp_path, tokens=tokens)
    tokens.before_push.append(mac_race(remote, {"ideas/Beta.md": "# Beta\n\nMac.\n"}))
    with pytest.raises(VaultError):
        svc.edit_note(
            OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), body="# Beta\n\nLost.\n"
        )

    svc.append_to_note(OWNER, "notes/Gamma.md", "Next call.")

    assert last_commit_paths(remote) == {"notes/Gamma.md"}
    assert remote.show("ideas/Beta.md") == "# Beta\n\nMac.\n"


# --- VAULT-WRITE-007: confinement -----------------------------------------------------------


def _mac_symlink(remote: Remote, link: str, target: str) -> None:
    git(remote.mac, "pull", "--rebase", "-q")
    path = remote.mac / link
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, path)
    git(remote.mac, "add", link)
    git(remote.mac, "commit", "-q", "-m", f"symlink {link}")
    git(remote.mac, "push", "-q", "origin", remote.branch)


# @spec VAULT-WRITE-007
@pytest.mark.parametrize(
    "path",
    [
        "/ideas/Abs.md",
        "../Outside.md",
        "ideas/../../Outside.md",
        "ideas/../notes/Sneaky.md",
        ".hidden/New.md",
        "ideas/.obsidian/New.md",
        "ideas/NoExtension",
        "ideas/Wrong.txt",
    ],
)
def test_create_note_refuses_unconfined_path(
    service: VaultService, remote: Remote, path: str
) -> None:
    head = remote.head()
    with pytest.raises(VaultError) as exc:
        service.create_note(OWNER, path, "# Nope\n")
    assert exc.value.code == "confinement"
    assert remote.head() == head


# @spec VAULT-WRITE-007
@pytest.mark.parametrize(
    "new_path", ["/notes/Abs.md", "../Twin.md", ".hidden/Twin.md", "notes/Twin.txt"]
)
def test_move_note_refuses_unconfined_destination(
    service: VaultService, remote: Remote, new_path: str
) -> None:
    head = remote.head()
    with pytest.raises(VaultError) as exc:
        service.move_note(OWNER, "ideas/Twin.md", blob(remote, "ideas/Twin.md"), new_path)
    assert exc.value.code == "confinement"
    assert remote.head() == head


# @spec VAULT-WRITE-007
def test_changing_call_on_note_in_dot_directory_is_confinement(
    service: VaultService, remote: Remote
) -> None:
    with pytest.raises(VaultError) as exc:
        service.append_to_note(OWNER, ".hidden/Secret.md", "x")
    assert exc.value.code == "confinement"
    assert remote.show(".hidden/Secret.md") == SEED[".hidden/Secret.md"]


# @spec VAULT-WRITE-007
def test_create_through_symlinked_directory_is_confinement(remote: Remote, tmp_path: Path) -> None:
    _mac_symlink(remote, "ideas/linked", "../notes")
    svc = build(remote, tmp_path)
    head = remote.head()
    with pytest.raises(VaultError) as exc:
        svc.create_note(OWNER, "ideas/linked/New.md", "# New\n")
    assert exc.value.code == "confinement"
    assert remote.head() == head


# @spec VAULT-WRITE-007
def test_append_to_symlinked_note_is_confinement(remote: Remote, tmp_path: Path) -> None:
    _mac_symlink(remote, "ideas/Alias.md", "Beta.md")
    svc = build(remote, tmp_path)
    with pytest.raises(VaultError) as exc:
        svc.append_to_note(OWNER, "ideas/Alias.md", "via the link")
    assert exc.value.code == "confinement"
    assert remote.show("ideas/Beta.md") == SEED["ideas/Beta.md"]


# @spec VAULT-WRITE-007
def test_create_outside_configured_subdirectory_is_confinement(
    remote: Remote, tmp_path: Path
) -> None:
    svc = build(remote, tmp_path, subdirectory="ideas")
    with pytest.raises(VaultError) as exc:
        svc.create_note(OWNER, "../notes/Escaped.md", "# Escaped\n")
    assert exc.value.code == "confinement"
    assert remote.show("notes/Escaped.md") is None


# @spec VAULT-WRITE-007
def test_create_inside_configured_subdirectory_lands_beneath_it(
    remote: Remote, tmp_path: Path
) -> None:
    """Counterpart to the refusal: vault-relative paths are relative to the subdirectory."""
    svc = build(remote, tmp_path, subdirectory="ideas")
    svc.create_note(OWNER, "Inside.md", "# Inside\n")
    assert "ideas/Inside.md" in remote.files_lines()


# --- VAULT-WRITE-008 / 009: size cap --------------------------------------------------------


# @spec VAULT-WRITE-008
def test_append_past_configured_cap_is_too_large(remote: Remote, tmp_path: Path) -> None:
    svc = build(remote, tmp_path, size_cap_bytes=1000)
    head = remote.head()
    with pytest.raises(VaultError) as exc:
        svc.append_to_note(OWNER, "notes/Gamma.md", "x" * 1000)
    assert exc.value.code == "too_large"
    assert remote.head() == head


# @spec VAULT-WRITE-008
def test_create_under_configured_cap_succeeds(remote: Remote, tmp_path: Path) -> None:
    svc = build(remote, tmp_path, size_cap_bytes=1000)
    svc.create_note(OWNER, "ideas/Small.md", "y" * 500)
    assert "ideas/Small.md" in remote.files_lines()


# @spec VAULT-WRITE-008
def test_link_rewriting_past_cap_is_too_large(remote: Remote, tmp_path: Path) -> None:
    """Links.md is 810 bytes; renaming Beta to a 23-letter name grows each of its 90 links."""
    links = "[[Beta]]\n" * 90
    remote.mac_commit({"notes/Links.md": links})
    svc = build(remote, tmp_path, size_cap_bytes=1000)
    head = remote.head()

    with pytest.raises(VaultError) as exc:
        svc.move_note(
            OWNER,
            "ideas/Beta.md",
            blob(remote, "ideas/Beta.md"),
            "ideas/BetaRenamedWithLongName.md",
        )

    assert exc.value.code == "too_large"
    assert remote.head() == head
    assert remote.show("notes/Links.md") == links


# @spec VAULT-WRITE-009
def test_default_size_cap_is_one_megabyte(remote: Remote, tmp_path: Path) -> None:
    settings = make_settings(remote, tmp_path / "w")
    assert settings.size_cap_bytes == 1_000_000


# @spec VAULT-WRITE-009
def test_default_cap_refuses_note_over_one_megabyte(service: VaultService, remote: Remote) -> None:
    with pytest.raises(VaultError) as exc:
        service.create_note(OWNER, "ideas/Huge.md", "z" * 1_048_577)
    assert exc.value.code == "too_large"
    assert remote.show("ideas/Huge.md") is None


# @spec VAULT-WRITE-009
def test_default_cap_admits_note_just_under_one_megabyte(
    service: VaultService, remote: Remote
) -> None:
    service.create_note(OWNER, "ideas/Big.md", "z" * 999_000)
    assert "ideas/Big.md" in remote.files_lines()


# --- VAULT-ID-003: commit author ------------------------------------------------------------


# @spec VAULT-ID-003
def test_commit_author_is_callers_identity(remote: Remote, tmp_path: Path) -> None:
    svc = build(remote, tmp_path, identities=(OWNER, OTHER))
    svc.create_note(OTHER, "ideas/ByOther.md", "# By other\n")
    commit = remote.last_commit()
    assert commit["author_name"] == "Other Writer"
    assert commit["author_email"] == "other.commits@example.com"


# @spec VAULT-ID-003
def test_link_rewrites_in_other_notes_carry_callers_author(
    service: VaultService, remote: Remote
) -> None:
    service.move_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), "ideas/Beta2.md")
    for path in ("ideas/Alpha.md", "notes/Gamma.md"):
        author = git(
            remote.bare, "log", "-1", "--format=%an <%ae>", remote.branch, "--", path
        ).strip()
        assert author == f"{OWNER.author_name} <{OWNER.author_email}>", path


# @spec VAULT-ID-003
def test_commit_author_survives_push_race_retry(remote: Remote, tmp_path: Path) -> None:
    tokens = RacingTokens()
    svc = build(remote, tmp_path, tokens=tokens, identities=(OWNER, OTHER))
    tokens.before_push.append(mac_race(remote, {"notes/Gamma.md": "# G\n"}))
    svc.append_to_note(OTHER, "ideas/Beta.md", "after the race")
    commit = remote.last_commit()
    assert (commit["author_name"], commit["author_email"]) == (
        "Other Writer",
        "other.commits@example.com",
    )
