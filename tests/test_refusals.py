"""Configured refusals (VAULT-REFUSE-*) and the refusal-source credential (VAULT-ID-005)."""

from __future__ import annotations

import logging
import unicodedata
from typing import TYPE_CHECKING, Any

import pytest

from obsidian_git_mcp.errors import VaultError
from obsidian_git_mcp.service import VaultService
from tests.conftest import (
    GUARD_WORD,
    OWNER,
    PINNED_LIST,
    REFUSAL_TEXT,
    SEED,
    Clock,
    FakeRefusals,
    FakeTokens,
    Remote,
    git,
    make_settings,
)

if TYPE_CHECKING:
    from pathlib import Path

GUARD_PATTERN = REFUSAL_TEXT["guards.yaml"].strip()
ALL_SOURCES = {"superseded.yaml", "pins.txt", "guards.yaml"}


# --- helpers ----------------------------------------------------------------------------------


def blob(remote: Remote, path: str) -> str:
    """The git blob id of `path` at the remote branch tip: what the service calls `revision`."""
    return git(remote.bare, "rev-parse", f"{remote.branch}:{path}").strip()


def build(
    remote: Remote,
    tmp_path: Path,
    *,
    tokens: FakeTokens | None = None,
    refusals: FakeRefusals | None = None,
    name: str = "work-custom",
    **overrides: Any,
) -> VaultService:
    settings = make_settings(remote, tmp_path / name, **overrides)
    svc = VaultService(settings, tokens or FakeTokens(), refusals or FakeRefusals(), clock=Clock())
    svc.start()
    return svc


def refused(code: str, call: Any, *args: Any, **kwargs: Any) -> VaultError:
    with pytest.raises(VaultError) as exc:
        call(*args, **kwargs)
    assert exc.value.code == code
    return exc.value


# --- VAULT-REFUSE-001: read at every write --------------------------------------------------


# @spec VAULT-REFUSE-001
def test_changing_call_reads_every_refusal_source(
    service: VaultService, refusals: FakeRefusals
) -> None:
    refusals.reads.clear()
    service.append_to_note(OWNER, "ideas/Beta.md", "more")
    assert set(refusals.reads) == ALL_SOURCES


# @spec VAULT-REFUSE-001
def test_each_changing_call_rereads_refusal_sources(
    service: VaultService, refusals: FakeRefusals
) -> None:
    refusals.reads.clear()
    service.append_to_note(OWNER, "ideas/Beta.md", "one")
    service.append_to_note(OWNER, "ideas/Beta.md", "two")
    assert refusals.reads.count("pins.txt") == 2
    assert refusals.reads.count("guards.yaml") == 2


# @spec VAULT-REFUSE-001
def test_refusal_list_changed_after_start_applies_to_next_call(
    service: VaultService, refusals: FakeRefusals, remote: Remote
) -> None:
    refusals.text["pins.txt"] += "ideas/Twin.md\n"
    refused(
        "refused_pinned",
        service.delete_note,
        OWNER,
        "ideas/Twin.md",
        blob(remote, "ideas/Twin.md"),
    )
    assert remote.show("ideas/Twin.md") == SEED["ideas/Twin.md"]


# --- VAULT-REFUSE-002: extraction -----------------------------------------------------------


# @spec VAULT-REFUSE-002
def test_yaml_mapping_key_with_prefix_is_pinned(service: VaultService, remote: Remote) -> None:
    refused(
        "refused_pinned",
        service.delete_note,
        OWNER,
        "ideas/Pinned.md",
        blob(remote, "ideas/Pinned.md"),
    )


# @spec VAULT-REFUSE-002
def test_yaml_mapping_value_with_prefix_is_pinned(
    service: VaultService, refusals: FakeRefusals, remote: Remote
) -> None:
    refusals.text["superseded.yaml"] += "datapad/notes/Old.md: datapad/notes/Gamma.md\n"
    refused(
        "refused_pinned",
        service.delete_note,
        OWNER,
        "notes/Gamma.md",
        blob(remote, "notes/Gamma.md"),
    )


# @spec VAULT-REFUSE-002
def test_yaml_mapping_entries_without_prefix_are_ignored(remote: Remote, tmp_path: Path) -> None:
    """`other/a.md: other/b.md` and the value `homelab-batch/docs/x.md` lack `datapad/`."""
    remote.mac_commit(
        {
            "other/a.md": "# a\n",
            "other/b.md": "# b\n",
            "homelab-batch/docs/x.md": "# x\n",
        }
    )
    svc = build(remote, tmp_path)
    for path in ("other/a.md", "other/b.md", "homelab-batch/docs/x.md"):
        svc.delete_note(OWNER, path, blob(remote, path))
        assert remote.show(path) is None, path


# @spec VAULT-REFUSE-002
def test_yaml_mapping_prefix_is_removed_not_matched_literally(
    remote: Remote, tmp_path: Path
) -> None:
    """A note at `datapad/ideas/Pinned.md` is not the pinned `ideas/Pinned.md`."""
    remote.mac_commit({"datapad/ideas/Pinned.md": "# decoy\n"})
    svc = build(remote, tmp_path)
    svc.delete_note(OWNER, "datapad/ideas/Pinned.md", blob(remote, "datapad/ideas/Pinned.md"))
    assert remote.show("datapad/ideas/Pinned.md") is None


# @spec VAULT-REFUSE-002
@pytest.mark.parametrize("path", ["README.md", "ideas/README.md", "notes/README.md"])
def test_plain_list_entries_are_pinned(service: VaultService, remote: Remote, path: str) -> None:
    refused("refused_pinned", service.delete_note, OWNER, path, blob(remote, path))


# @spec VAULT-REFUSE-002
def test_pins_are_union_of_all_sources(service: VaultService, remote: Remote) -> None:
    """ideas/Pinned.md comes from the mapping source, README.md from the list source."""
    refused("refused_pinned", service.delete_note, OWNER, "README.md", blob(remote, "README.md"))
    refused(
        "refused_pinned",
        service.delete_note,
        OWNER,
        "ideas/Pinned.md",
        blob(remote, "ideas/Pinned.md"),
    )


# @spec VAULT-REFUSE-002
def test_pins_come_only_from_configured_sources(remote: Remote, tmp_path: Path) -> None:
    """With only the list source configured, the mapping's pin no longer applies."""
    svc = build(remote, tmp_path, pinned_sources=(PINNED_LIST,))
    svc.delete_note(OWNER, "ideas/Pinned.md", blob(remote, "ideas/Pinned.md"))
    assert remote.show("ideas/Pinned.md") is None


# --- VAULT-REFUSE-003: pinned targets -------------------------------------------------------


# @spec VAULT-REFUSE-003
def test_move_of_pinned_note_is_refused(service: VaultService, remote: Remote) -> None:
    head = remote.head()
    refused(
        "refused_pinned",
        service.move_note,
        OWNER,
        "ideas/Pinned.md",
        blob(remote, "ideas/Pinned.md"),
        "notes/Pinned.md",
    )
    assert remote.head() == head


# @spec VAULT-REFUSE-003
def test_delete_of_pinned_note_is_refused(service: VaultService, remote: Remote) -> None:
    head = remote.head()
    refused(
        "refused_pinned",
        service.delete_note,
        OWNER,
        "ideas/Pinned.md",
        blob(remote, "ideas/Pinned.md"),
    )
    assert remote.head() == head
    assert remote.show("trash/ideas/Pinned.md") is None


# @spec VAULT-REFUSE-003
def test_archive_of_pinned_note_is_refused(service: VaultService, remote: Remote) -> None:
    head = remote.head()
    refused(
        "refused_pinned",
        service.archive_note,
        OWNER,
        "ideas/Pinned.md",
        blob(remote, "ideas/Pinned.md"),
        fields={"issue": "x#1"},
    )
    assert remote.head() == head
    assert remote.show("ideas/Pinned.md") == SEED["ideas/Pinned.md"]


# @spec VAULT-REFUSE-003
def test_pinned_note_addressed_by_name_is_refused(service: VaultService, remote: Remote) -> None:
    refused(
        "refused_pinned", service.delete_note, OWNER, "Pinned", blob(remote, "ideas/Pinned.md")
    )


# @spec VAULT-REFUSE-003
def test_pin_matches_case_folded_path(
    service: VaultService, refusals: FakeRefusals, remote: Remote
) -> None:
    refusals.text["pins.txt"] += "IDEAS/twin.MD\n"
    refused(
        "refused_pinned",
        service.delete_note,
        OWNER,
        "ideas/Twin.md",
        blob(remote, "ideas/Twin.md"),
    )


# @spec VAULT-REFUSE-003
def test_pin_matches_after_nfc_normalisation(remote: Remote, tmp_path: Path) -> None:
    nfc = unicodedata.normalize("NFC", "ideas/Café.md")
    nfd = unicodedata.normalize("NFD", "ideas/Café.md")
    assert nfc != nfd
    remote.mac_commit({nfc: "# Café\n"})
    refusals = FakeRefusals()
    refusals.text["pins.txt"] += nfd + "\n"
    svc = build(remote, tmp_path, refusals=refusals)
    note = next(p for p in remote.files_lines() if p.startswith("ideas/Caf"))
    refused("refused_pinned", svc.delete_note, OWNER, note, blob(remote, note))


# --- VAULT-REFUSE-004: pinned destinations --------------------------------------------------


# @spec VAULT-REFUSE-004
def test_create_at_pinned_path_is_refused(
    service: VaultService, refusals: FakeRefusals, remote: Remote
) -> None:
    refusals.text["pins.txt"] += "ideas/Reserved.md\n"
    refused("refused_pinned", service.create_note, OWNER, "ideas/Reserved.md", "# Reserved\n")
    assert remote.show("ideas/Reserved.md") is None


# @spec VAULT-REFUSE-004
def test_move_onto_equivalent_of_pinned_path_is_refused(
    service: VaultService, refusals: FakeRefusals, remote: Remote
) -> None:
    refusals.text["pins.txt"] += "ideas/Reserved.md\n"
    head = remote.head()
    refused(
        "refused_pinned",
        service.move_note,
        OWNER,
        "notes/Twin.md",
        blob(remote, "notes/Twin.md"),
        "ideas/reserved.md",
    )
    assert remote.head() == head


# --- VAULT-REFUSE-005: pins naming no note --------------------------------------------------


# @spec VAULT-REFUSE-005
def test_pin_naming_no_note_logs_warning_and_call_succeeds(
    service: VaultService,
    refusals: FakeRefusals,
    remote: Remote,
    caplog: pytest.LogCaptureFixture,
) -> None:
    refusals.text["pins.txt"] += "ideas/Gone.md\n"
    before = remote.commits()
    with caplog.at_level(logging.WARNING):
        service.append_to_note(OWNER, "ideas/Beta.md", "still works")
    assert remote.commits() == before + 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ideas/Gone.md" in r.getMessage() for r in warnings)


# --- VAULT-REFUSE-006: guard patterns -------------------------------------------------------


# @spec VAULT-REFUSE-006
def test_guard_match_in_new_body_is_refused(service: VaultService, remote: Remote) -> None:
    head = remote.head()
    refused("refused_guard", service.create_note, OWNER, "ideas/New.md", f"About {GUARD_WORD}.\n")
    assert remote.head() == head


# @spec VAULT-REFUSE-006
def test_guard_match_in_new_path_is_refused(service: VaultService, remote: Remote) -> None:
    refused("refused_guard", service.create_note, OWNER, f"ideas/{GUARD_WORD}.md", "# Clean\n")
    assert remote.show(f"ideas/{GUARD_WORD}.md") is None


# @spec VAULT-REFUSE-006
def test_guard_match_in_move_destination_is_refused(service: VaultService, remote: Remote) -> None:
    head = remote.head()
    refused(
        "refused_guard",
        service.move_note,
        OWNER,
        "ideas/Twin.md",
        blob(remote, "ideas/Twin.md"),
        f"ideas/{GUARD_WORD}.md",
    )
    assert remote.head() == head


# @spec VAULT-REFUSE-006
def test_guard_match_in_frontmatter_field_is_refused(
    service: VaultService, remote: Remote
) -> None:
    refused(
        "refused_guard",
        service.edit_note,
        OWNER,
        "notes/Gamma.md",
        blob(remote, "notes/Gamma.md"),
        fields={"owner": GUARD_WORD},
    )
    assert remote.show("notes/Gamma.md") == SEED["notes/Gamma.md"]


# @spec VAULT-REFUSE-006
def test_existing_match_does_not_block_unrelated_edit(
    service: VaultService, remote: Remote
) -> None:
    service.append_to_note(OWNER, "notes/Legacy.md", "An unrelated line.")
    text = remote.show("notes/Legacy.md")
    assert text is not None
    assert "An unrelated line." in text
    assert text.count(GUARD_WORD) == 1


# @spec VAULT-REFUSE-006
def test_edit_adding_second_match_is_refused(service: VaultService, remote: Remote) -> None:
    refused(
        "refused_guard", service.append_to_note, OWNER, "notes/Legacy.md", f"And {GUARD_WORD}."
    )
    assert remote.show("notes/Legacy.md") == SEED["notes/Legacy.md"]


# @spec VAULT-REFUSE-006
def test_edit_keeping_match_count_level_is_allowed(service: VaultService, remote: Remote) -> None:
    body = f"# Legacy\n\n{GUARD_WORD} moved to another sentence.\n"
    service.edit_note(OWNER, "notes/Legacy.md", blob(remote, "notes/Legacy.md"), body=body)
    assert remote.show("notes/Legacy.md") == body


# @spec VAULT-REFUSE-006
def test_moving_note_that_already_holds_match_is_allowed(
    service: VaultService, remote: Remote
) -> None:
    service.move_note(OWNER, "notes/Legacy.md", blob(remote, "notes/Legacy.md"), "notes/Old.md")
    assert remote.show("notes/Old.md") == SEED["notes/Legacy.md"]
    assert remote.show("notes/Legacy.md") is None


# --- VAULT-REFUSE-007: what a guard refusal reveals -----------------------------------------


# @spec VAULT-REFUSE-007
def test_guard_refusal_reports_number_of_added_matches(service: VaultService) -> None:
    err = refused(
        "refused_guard",
        service.create_note,
        OWNER,
        "ideas/New.md",
        f"{GUARD_WORD} and {GUARD_WORD} again.\n",
    )
    assert 2 in err.detail.values()


# @spec VAULT-REFUSE-007
def test_guard_refusal_response_omits_pattern_and_match(service: VaultService) -> None:
    err = refused(
        "refused_guard", service.create_note, OWNER, "ideas/New.md", f"About {GUARD_WORD}.\n"
    )
    for text in (repr(err.detail), str(err), repr(err.args)):
        assert GUARD_WORD not in text
        assert GUARD_PATTERN not in text


# @spec VAULT-REFUSE-007
def test_guard_refusal_logs_omit_pattern_and_match(
    service: VaultService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        refused(
            "refused_guard", service.create_note, OWNER, "ideas/New.md", f"About {GUARD_WORD}.\n"
        )
    assert caplog.records, "the refusal should be audited"
    for record in caplog.records:
        dumped = repr(record.__dict__) + record.getMessage()
        assert GUARD_WORD not in dumped
        assert GUARD_PATTERN not in dumped


# --- VAULT-REFUSE-008: Python re, no flags --------------------------------------------------


# @spec VAULT-REFUSE-008
def test_guard_is_case_sensitive(service: VaultService, remote: Remote) -> None:
    service.create_note(OWNER, "ideas/New.md", f"About {GUARD_WORD.lower()}.\n")
    assert remote.show("ideas/New.md") is not None


# @spec VAULT-REFUSE-008
@pytest.mark.parametrize(
    ("body", "is_refused"), [("Volde-mort\n", True), ("Volde\nmort\n", False)]
)
def test_guard_dot_does_not_match_newline(
    remote: Remote, tmp_path: Path, body: str, is_refused: bool
) -> None:
    refusals = FakeRefusals()
    refusals.text["guards.yaml"] = "Volde.mort\n"
    svc = build(remote, tmp_path, refusals=refusals)
    if is_refused:
        refused("refused_guard", svc.create_note, OWNER, "ideas/New.md", body)
    else:
        svc.create_note(OWNER, "ideas/New.md", body)
        assert remote.show("ideas/New.md") is not None


# @spec VAULT-REFUSE-008
@pytest.mark.parametrize(("body", "is_refused"), [("xabab\n", True), ("ab ab\n", False)])
def test_guard_uses_python_named_backreference_syntax(
    remote: Remote, tmp_path: Path, body: str, is_refused: bool
) -> None:
    """`(?P<w>..)(?P=w)` is Python-only syntax (RE2 has no backreferences, PCRE spells it
    differently), so honouring it shows the pattern is compiled by `re`."""
    refusals = FakeRefusals()
    refusals.text["guards.yaml"] = "(?P<w>ab)(?P=w)\n"
    svc = build(remote, tmp_path, refusals=refusals)
    if is_refused:
        refused("refused_guard", svc.create_note, OWNER, "ideas/New.md", body)
    else:
        svc.create_note(OWNER, "ideas/New.md", body)
        assert remote.show("ideas/New.md") is not None


# @spec VAULT-REFUSE-008
def test_guard_word_boundary_follows_python_semantics(
    service: VaultService, remote: Remote
) -> None:
    """`\\bVoldemort\\b` does not match inside `Voldemorts`."""
    service.create_note(OWNER, "ideas/New.md", f"Many {GUARD_WORD}s.\n")
    assert remote.show("ideas/New.md") is not None


# --- VAULT-REFUSE-009: unconfigured sources -------------------------------------------------


# @spec VAULT-REFUSE-009
def test_no_pinned_source_means_nothing_pinned(remote: Remote, tmp_path: Path) -> None:
    refusals = FakeRefusals(text={})
    svc = build(remote, tmp_path, refusals=refusals, pinned_sources=(), guard_source=None)
    svc.delete_note(OWNER, "README.md", blob(remote, "README.md"))
    assert remote.show("README.md") is None
    assert remote.show("trash/README.md") == SEED["README.md"]


# @spec VAULT-REFUSE-009
def test_no_guard_source_means_nothing_guarded(remote: Remote, tmp_path: Path) -> None:
    refusals = FakeRefusals(text={})
    svc = build(remote, tmp_path, refusals=refusals, pinned_sources=(), guard_source=None)
    svc.create_note(OWNER, "ideas/New.md", f"About {GUARD_WORD}.\n")
    assert remote.show("ideas/New.md") is not None
    assert refusals.reads == []


# --- VAULT-REFUSE-010: unreadable sources ---------------------------------------------------


def _changing_calls(svc: VaultService, remote: Remote) -> dict[str, Any]:
    """One valid invocation of every changing tool, each otherwise certain to succeed."""
    return {
        "create_note": lambda: svc.create_note(OWNER, "ideas/New.md", "# New\n"),
        "append_to_note": lambda: svc.append_to_note(OWNER, "ideas/Beta.md", "more"),
        "edit_note": lambda: svc.edit_note(
            OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), body="# B\n"
        ),
        "move_note": lambda: svc.move_note(
            OWNER, "ideas/Twin.md", blob(remote, "ideas/Twin.md"), "ideas/Twin2.md"
        ),
        "delete_note": lambda: svc.delete_note(
            OWNER, "notes/Twin.md", blob(remote, "notes/Twin.md")
        ),
        "archive_note": lambda: svc.archive_note(
            OWNER, "Deep.md", blob(remote, "Deep.md"), fields={"issue": "x#1"}
        ),
    }


# @spec VAULT-REFUSE-010
@pytest.mark.parametrize("source", sorted(ALL_SOURCES))
@pytest.mark.parametrize(
    "tool",
    ["create_note", "append_to_note", "edit_note", "move_note", "delete_note", "archive_note"],
)
def test_unreadable_source_at_runtime_refuses_changing_calls(
    service: VaultService, refusals: FakeRefusals, remote: Remote, source: str, tool: str
) -> None:
    refusals.unreadable.add(source)
    head = remote.head()
    refused("refusal_source_unavailable", _changing_calls(service, remote)[tool])
    assert remote.head() == head


# @spec VAULT-REFUSE-010
def test_unreadable_source_at_startup_does_not_stop_start(remote: Remote, tmp_path: Path) -> None:
    refusals = FakeRefusals(unreadable={"guards.yaml"})
    svc = build(remote, tmp_path, refusals=refusals)
    refused("refusal_source_unavailable", svc.append_to_note, OWNER, "ideas/Beta.md", "x")


# @spec VAULT-REFUSE-010
def test_reads_served_while_source_unreadable(remote: Remote, tmp_path: Path) -> None:
    refusals = FakeRefusals(unreadable={"pins.txt", "superseded.yaml", "guards.yaml"})
    svc = build(remote, tmp_path, refusals=refusals)
    assert svc.read_note(OWNER, "ideas/Beta.md")["path"] == "ideas/Beta.md"
    assert svc.list_notes(OWNER)["total"] > 0
    assert svc.search_notes(OWNER, query="Design")["total"] > 0


# @spec VAULT-REFUSE-010
def test_writes_resume_when_source_readable_again(
    service: VaultService, refusals: FakeRefusals, remote: Remote
) -> None:
    refusals.unreadable.add("pins.txt")
    refused("refusal_source_unavailable", service.append_to_note, OWNER, "ideas/Beta.md", "x")
    refusals.unreadable.clear()
    service.append_to_note(OWNER, "ideas/Beta.md", "after recovery")
    beta = remote.show("ideas/Beta.md")
    assert beta is not None
    assert "after recovery" in beta


# --- VAULT-ID-005: the refusal-source credential --------------------------------------------


# @spec VAULT-ID-005
def test_refusal_sources_are_read_without_vault_tokens(remote: Remote, tmp_path: Path) -> None:
    """Refusal text comes only through the RefusalReader port (which holds the refusal-source
    credential). If the service minted a vault token to read refusal sources, the same call
    would mint more vault tokens with sources configured than without."""
    tokens_with, refusals_with = FakeTokens(), FakeRefusals()
    tokens_without, refusals_without = FakeTokens(), FakeRefusals(text={})
    with_sources = build(remote, tmp_path, tokens=tokens_with, refusals=refusals_with, name="a")
    without = build(
        remote,
        tmp_path,
        tokens=tokens_without,
        refusals=refusals_without,
        name="b",
        pinned_sources=(),
        guard_source=None,
    )
    tokens_with.calls.clear()
    tokens_without.calls.clear()
    refusals_with.reads.clear()

    with_sources.append_to_note(OWNER, "ideas/Beta.md", "one")
    without.append_to_note(OWNER, "ideas/Beta.md", "two")

    assert set(refusals_with.reads) == ALL_SOURCES
    assert tokens_with.calls == tokens_without.calls
