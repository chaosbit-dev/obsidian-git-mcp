"""The list-under-key extraction (`yaml-list`): VAULT-REFUSE-002, -011 and -012.

The guard file the estate reads is a YAML mapping whose `patterns` key holds the list, preceded by
comment lines. `GUARDS_FILE` reproduces that shape: comments carrying unbalanced parentheses and a
mid-line `(?i)`, and list items with an inline `(?i)` and look-around bounds.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from obsidian_git_mcp import settings_env
from obsidian_git_mcp.config import RefusalSource
from obsidian_git_mcp.errors import VaultError
from obsidian_git_mcp.refusal_lists import SourceUnusable, entries
from obsidian_git_mcp.service import VaultService
from tests.conftest import (
    GUARD_WORD,
    OWNER,
    REFUSAL_TEXT,
    Clock,
    FakeRefusals,
    FakeTokens,
    Remote,
    git,
    make_settings,
)

if TYPE_CHECKING:
    from pathlib import Path

GUARDS_YAML_LIST = RefusalSource("guards.yaml", "yaml-list", key="patterns")
WORD = GUARD_WORD.lower()
GUARDS_FILE = (
    "# Exclusion guards. The second line, never the boundary.\n"
    "#\n"
    "# CASE. Every pattern carries an inline `(?i)`, because the consumer calls\n"
    "# `re.search` with no flags (and there is nowhere else to put one.\n"
    "\n"
    "patterns:\n"
    f"  - '(?i)(?<![a-z0-9]){WORD}(?![a-z0-9])'\n"
    "  - '(?i)(?<![a-z0-9])grindelwald(?![a-z0-9])'\n"
)


def blob(remote: Remote, path: str) -> str:
    return git(remote.bare, "rev-parse", f"{remote.branch}:{path}").strip()


def build(remote: Remote, tmp_path: Path, text: str, **overrides: Any) -> VaultService:
    refusals = FakeRefusals(text={**REFUSAL_TEXT, "guards.yaml": text})
    overrides.setdefault("guard_source", GUARDS_YAML_LIST)
    settings = make_settings(remote, tmp_path / "work-yaml-list", **overrides)
    svc = VaultService(settings, FakeTokens(), refusals, clock=Clock())
    svc.start()
    return svc


def refused(code: str, call: Any, *args: Any) -> None:
    with pytest.raises(VaultError) as exc:
        call(*args)
    assert exc.value.code == code


# --- extraction ----------------------------------------------------------------------------


# @spec VAULT-REFUSE-011
def test_entries_are_the_items_under_the_key() -> None:
    assert entries(GUARDS_YAML_LIST, GUARDS_FILE) == [
        f"(?i)(?<![a-z0-9]){WORD}(?![a-z0-9])",
        "(?i)(?<![a-z0-9])grindelwald(?![a-z0-9])",
    ]


# @spec VAULT-REFUSE-011
def test_other_keys_are_ignored() -> None:
    text = "other: [ignored]\npatterns: [kept]\n"
    assert entries(GUARDS_YAML_LIST, text) == ["kept"]


# @spec VAULT-REFUSE-011
def test_guard_file_with_comments_refuses_a_guarded_write(remote: Remote, tmp_path: Path) -> None:
    svc = build(remote, tmp_path, GUARDS_FILE)
    head = remote.head()
    refused("refused_guard", svc.create_note, OWNER, "ideas/New.md", f"About {GUARD_WORD}.\n")
    assert remote.head() == head


# @spec VAULT-REFUSE-011
def test_guard_file_with_comments_lets_a_clean_write_through(
    remote: Remote, tmp_path: Path
) -> None:
    svc = build(remote, tmp_path, GUARDS_FILE)
    svc.create_note(OWNER, "ideas/New.md", "# Nothing guarded here\n")
    assert remote.show("ideas/New.md") == "# Nothing guarded here\n"


# @spec VAULT-REFUSE-011
def test_inline_flags_and_bounds_apply(remote: Remote, tmp_path: Path) -> None:
    """`(?i)` makes the case irrelevant; the bounds reject the word inside an identifier but
    accept it in a `Given_Surname` label."""
    svc = build(remote, tmp_path, GUARDS_FILE)
    refused("refused_guard", svc.create_note, OWNER, "ideas/A.md", f"{GUARD_WORD.upper()}\n")
    refused("refused_guard", svc.create_note, OWNER, "ideas/B.md", f"{GUARD_WORD}_Riddle\n")
    svc.create_note(OWNER, "ideas/C.md", f"x{WORD}2\n")
    assert remote.show("ideas/C.md") is not None


# @spec VAULT-REFUSE-002
def test_pinned_paths_can_come_from_a_list_under_a_key(remote: Remote, tmp_path: Path) -> None:
    pins = RefusalSource("pins.yaml", "yaml-list", key="pinned")
    refusals = FakeRefusals(text={**REFUSAL_TEXT, "pins.yaml": "pinned:\n  - notes/Twin.md\n"})
    settings = make_settings(remote, tmp_path / "work-pins", pinned_sources=(pins,))
    svc = VaultService(settings, FakeTokens(), refusals, clock=Clock())
    svc.start()
    twin = blob(remote, "notes/Twin.md")
    refused("refused_pinned", svc.delete_note, OWNER, "notes/Twin.md", twin)


# --- unusable shapes fail closed -------------------------------------------------------------

UNUSABLE = {
    "empty file": "",
    "comments only": "# nothing here\n",
    "not yaml": "patterns: [unclosed\n",
    "top level is a list": "- a\n- b\n",
    "top level is a string": "just text\n",
    "key missing": "other: [a]\n",
    "key is null": "patterns:\n",
    "value is a string": "patterns: abc\n",
    "value is a mapping": "patterns: {a: b}\n",
    "empty list": "patterns: []\n",
    "non-string item": "patterns: [abc, 3]\n",
    "null item": "patterns: [abc, null]\n",
    "empty-string item": "patterns: [abc, '']\n",
    "nested list item": "patterns: [[abc]]\n",
}


# @spec VAULT-REFUSE-012
@pytest.mark.parametrize("text", UNUSABLE.values(), ids=UNUSABLE.keys())
def test_unusable_shape_raises(text: str) -> None:
    with pytest.raises(SourceUnusable):
        entries(GUARDS_YAML_LIST, text)


# @spec VAULT-REFUSE-012, VAULT-REFUSE-010
@pytest.mark.parametrize("text", UNUSABLE.values(), ids=UNUSABLE.keys())
def test_unusable_guard_source_refuses_every_write(
    remote: Remote, tmp_path: Path, text: str
) -> None:
    svc = build(remote, tmp_path, text)
    head = remote.head()
    refused("refusal_source_unavailable", svc.create_note, OWNER, "ideas/New.md", "# Clean\n")
    assert remote.head() == head


# @spec VAULT-REFUSE-012, VAULT-REFUSE-010
def test_unusable_pinned_source_refuses_every_write(remote: Remote, tmp_path: Path) -> None:
    pins = RefusalSource("pins.yaml", "yaml-list", key="pinned")
    refusals = FakeRefusals(text={**REFUSAL_TEXT, "pins.yaml": "pinned: []\n"})
    settings = make_settings(remote, tmp_path / "work-pins", pinned_sources=(pins,))
    svc = VaultService(settings, FakeTokens(), refusals, clock=Clock())
    svc.start()
    refused("refusal_source_unavailable", svc.create_note, OWNER, "ideas/New.md", "# Clean\n")


# @spec VAULT-REFUSE-010
def test_guard_pattern_that_does_not_compile_refuses_every_write(
    remote: Remote, tmp_path: Path
) -> None:
    svc = build(remote, tmp_path, "patterns: ['(unclosed']\n")
    refused("refusal_source_unavailable", svc.create_note, OWNER, "ideas/New.md", "# Clean\n")


# --- configuration ------------------------------------------------------------------------


def _source_model(**fields: Any) -> settings_env.RefusalSourceModel:
    return settings_env.RefusalSourceModel.model_validate(
        {"location": "chaosbit-dev/holocron:guards.yaml@main", **fields}
    )


# @spec VAULT-REFUSE-011
def test_yaml_list_with_key_is_accepted() -> None:
    model = _source_model(extraction="yaml-list", key="patterns")
    assert (model.extraction, model.key) == ("yaml-list", "patterns")


# @spec VAULT-REFUSE-011
def test_yaml_list_from_environment_json_reaches_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(
        f"{settings_env.ENV_PREFIX}GUARD_SOURCE",
        json.dumps(
            {
                "location": "chaosbit-dev/holocron:guards.yaml@main",
                "extraction": "yaml-list",
                "key": "patterns",
            }
        ),
    )
    monkeypatch.setenv(f"{settings_env.ENV_PREFIX}REMOTE_URL", "https://example.com/v.git")
    monkeypatch.setenv(f"{settings_env.ENV_PREFIX}ACCESS_TEAM_DOMAIN", "t")
    monkeypatch.setenv(f"{settings_env.ENV_PREFIX}ACCESS_AUDIENCE", "a")
    monkeypatch.setenv(f"{settings_env.ENV_PREFIX}GITHUB_APP_ID", "1")
    monkeypatch.setenv(f"{settings_env.ENV_PREFIX}GITHUB_INSTALLATION_ID", "2")
    monkeypatch.delenv(settings_env.CONFIG_FILE_ENV, raising=False)
    settings = settings_env.EnvSettings().to_settings()
    assert settings.guard_source == RefusalSource(
        "chaosbit-dev/holocron:guards.yaml@main", "yaml-list", key="patterns"
    )


# @spec VAULT-REFUSE-011
@pytest.mark.parametrize("key", [None, "", "  "])
def test_yaml_list_without_a_key_is_rejected(key: str | None) -> None:
    fields: dict[str, Any] = {"extraction": "yaml-list"}
    if key is not None:
        fields["key"] = key
    with pytest.raises(ValueError, match="key"):
        _source_model(**fields)


# @spec VAULT-REFUSE-011
@pytest.mark.parametrize("extraction", ["list", "yaml-mapping"])
def test_a_key_on_another_extraction_is_rejected(extraction: str) -> None:
    """A key the extraction would ignore is a misconfiguration, not a no-op."""
    with pytest.raises(ValueError, match="key"):
        _source_model(extraction=extraction, key="patterns")
