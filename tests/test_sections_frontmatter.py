"""Sections (named by heading) and frontmatter edits.

ideas/Beta.md carries the section fixtures: `# Beta` > `## Design` > `### Sub`, `## Notes`, two
`## Dup` headings, and a `## Design` inside a fenced block that must not count. ideas/Alpha.md
carries the frontmatter fixtures: a flow list, a quoted wikilink and a comment line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from ruamel.yaml import YAML

from obsidian_git_mcp.errors import VaultError
from tests.conftest import OWNER, SEED, git

if TYPE_CHECKING:
    from obsidian_git_mcp.service import VaultService
    from tests.conftest import Remote

BETA = SEED["ideas/Beta.md"]
ALPHA = SEED["ideas/Alpha.md"]


def blob(remote: Remote, path: str) -> str:
    """The git blob id of `path` at the remote branch's tip: what `revision` must equal."""
    return git(remote.bare, "rev-parse", f"{remote.branch}:{path}").strip()


def frontmatter_block(text: str) -> str:
    """The YAML between a note's opening and closing `---` lines."""
    assert text.startswith("---\n"), text
    return text[len("---\n") : text.index("\n---\n", 3) + 1]


def parsed_frontmatter(text: str) -> dict[str, Any]:
    loaded = YAML(typ="safe").load(frontmatter_block(text))
    assert isinstance(loaded, dict)
    return loaded


def shown(remote: Remote, path: str) -> str:
    text = remote.show(path)
    assert text is not None, f"{path} missing on the remote"
    return text


# --- which heading names a section ----------------------------------------------------------


# @spec VAULT-SECT-001
@pytest.mark.parametrize(("heading", "level"), [("Beta", "#"), ("Notes", "##"), ("Sub", "###")])
def test_section_is_found_at_any_heading_level(
    service: VaultService, remote: Remote, heading: str, level: str
) -> None:
    service.append_to_note(OWNER, "ideas/Beta.md", "Marker line.\n", under_heading=heading)

    text = shown(remote, "ideas/Beta.md")
    assert text.count("Marker line.") == 1
    assert text.index(f"{level} {heading}\n") < text.index("Marker line.")


# @spec VAULT-SECT-001
def test_section_heading_inside_a_fenced_block_is_ignored(
    service: VaultService, remote: Remote
) -> None:
    """Beta has a second `## Design` inside a fence; were it counted, the call would be
    ambiguous."""
    service.append_to_note(OWNER, "ideas/Beta.md", "Marker line.\n", under_heading="Design")

    text = shown(remote, "ideas/Beta.md")
    assert text.index("Design text.") < text.index("Marker line.") < text.index("## Notes")


# @spec VAULT-SECT-001, VAULT-SECT-003
def test_section_heading_must_match_exactly(service: VaultService, remote: Remote) -> None:
    before = remote.head()

    with pytest.raises(VaultError) as exc:
        service.append_to_note(OWNER, "ideas/Beta.md", "Marker line.\n", under_heading="design")

    assert exc.value.code == "not_found"
    assert remote.head() == before


# @spec VAULT-SECT-001, VAULT-SECT-003
def test_section_heading_is_not_matched_by_a_prefix(service: VaultService, remote: Remote) -> None:
    with pytest.raises(VaultError) as exc:
        service.append_to_note(OWNER, "ideas/Beta.md", "Marker line.\n", under_heading="Des")

    assert exc.value.code == "not_found"


# --- how far a section runs -----------------------------------------------------------------


# @spec VAULT-SECT-002
def test_section_includes_its_lower_level_subsections(
    service: VaultService, remote: Remote
) -> None:
    service.edit_note(
        OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), body="Only.\n", section="Design"
    )

    text = shown(remote, "ideas/Beta.md")
    assert "Design text." not in text
    assert "### Sub" not in text
    assert "Sub text." not in text
    assert text.index("## Design\n") < text.index("Only.") < text.index("## Notes")
    assert text.endswith(BETA[BETA.index("## Notes") :])


# @spec VAULT-SECT-002
def test_section_ends_at_the_next_heading_of_a_higher_level(
    service: VaultService, remote: Remote
) -> None:
    """`### Sub` ends at `## Notes`, which is higher, not at the end of the note."""
    service.edit_note(
        OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), body="Only.\n", section="Sub"
    )

    text = shown(remote, "ideas/Beta.md")
    assert "Sub text." not in text
    assert text.index("### Sub\n") < text.index("Only.") < text.index("## Notes")
    assert text.startswith(BETA[: BETA.index("### Sub\n") + len("### Sub\n")])
    assert text.endswith(BETA[BETA.index("## Notes") :])


# @spec VAULT-SECT-002
def test_section_ends_at_the_next_heading_of_the_same_level(
    service: VaultService, remote: Remote
) -> None:
    service.append_to_note(OWNER, "ideas/Beta.md", "Marker line.\n", under_heading="Notes")

    text = shown(remote, "ideas/Beta.md")
    assert text.index("Notes text.") < text.index("Marker line.") < text.index("## Dup")


# @spec VAULT-SECT-002
def test_section_with_no_later_heading_of_its_level_runs_to_the_end_of_the_note(
    service: VaultService, remote: Remote
) -> None:
    """`# Beta` is the only level-1 heading, so its section is the whole note, fence included."""
    service.append_to_note(OWNER, "ideas/Beta.md", "Marker line.\n", under_heading="Beta")

    text = shown(remote, "ideas/Beta.md")
    assert text.startswith(BETA)
    assert text.rstrip("\n").endswith("Marker line.")


# --- heading errors -------------------------------------------------------------------------


# @spec VAULT-SECT-003
def test_append_under_a_missing_heading_fails_not_found_and_does_not_fall_back(
    service: VaultService, remote: Remote
) -> None:
    before = remote.head()

    with pytest.raises(VaultError) as exc:
        service.append_to_note(OWNER, "ideas/Beta.md", "Marker line.\n", under_heading="Nowhere")

    assert exc.value.code == "not_found"
    assert remote.head() == before
    assert shown(remote, "ideas/Beta.md") == BETA


# @spec VAULT-SECT-003
def test_append_under_a_heading_matching_twice_fails_ambiguous(
    service: VaultService, remote: Remote
) -> None:
    before = remote.head()

    with pytest.raises(VaultError) as exc:
        service.append_to_note(OWNER, "ideas/Beta.md", "Marker line.\n", under_heading="Dup")

    assert exc.value.code == "ambiguous"
    assert remote.head() == before


# @spec VAULT-SECT-003
def test_edit_of_a_missing_section_fails_not_found(service: VaultService, remote: Remote) -> None:
    before = remote.head()

    with pytest.raises(VaultError) as exc:
        service.edit_note(
            OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), body="X\n", section="Nowhere"
        )

    assert exc.value.code == "not_found"
    assert remote.head() == before


# @spec VAULT-SECT-003
def test_edit_of_a_section_matching_twice_fails_ambiguous(
    service: VaultService, remote: Remote
) -> None:
    before = remote.head()

    with pytest.raises(VaultError) as exc:
        service.edit_note(
            OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"), body="X\n", section="Dup"
        )

    assert exc.value.code == "ambiguous"
    assert remote.head() == before
    assert shown(remote, "ideas/Beta.md") == BETA


# --- frontmatter: change only what was given ------------------------------------------------


# @spec VAULT-FM-001
def test_setting_one_field_leaves_every_other_byte_of_the_frontmatter(
    service: VaultService, remote: Remote
) -> None:
    """The flow list, the quoted wikilink and the comment all survive verbatim."""
    service.edit_note(
        OWNER, "ideas/Alpha.md", blob(remote, "ideas/Alpha.md"), fields={"status": "active"}
    )

    assert shown(remote, "ideas/Alpha.md") == ALPHA.replace("status: idea", "status: active")


# @spec VAULT-FM-001
def test_adding_a_new_field_keeps_existing_keys_order_and_comments(
    service: VaultService, remote: Remote
) -> None:
    service.edit_note(
        OWNER, "ideas/Alpha.md", blob(remote, "ideas/Alpha.md"), fields={"owner": "brandon"}
    )

    text = shown(remote, "ideas/Alpha.md")
    block = frontmatter_block(text)
    original_block = frontmatter_block(ALPHA)
    assert [line for line in block.splitlines() if not line.startswith("owner:")] == (
        original_block.splitlines()
    )
    assert "owner: brandon" in block.splitlines()
    assert text.endswith(ALPHA[ALPHA.index("\n---\n", 3) :])


# @spec VAULT-FM-001
def test_setting_a_field_on_a_note_without_frontmatter_leaves_the_body(
    service: VaultService, remote: Remote
) -> None:
    service.edit_note(OWNER, "Deep.md", blob(remote, "Deep.md"), fields={"status": "idea"})

    assert shown(remote, "Deep.md") == "---\nstatus: idea\n---\n" + SEED["Deep.md"]


# @spec VAULT-FM-002
def test_null_field_removes_the_key(service: VaultService, remote: Remote) -> None:
    service.edit_note(
        OWNER, "ideas/Alpha.md", blob(remote, "ideas/Alpha.md"), fields={"priority": None}
    )

    assert shown(remote, "ideas/Alpha.md") == ALPHA.replace("priority: P2\n", "")


# @spec VAULT-FM-002
def test_null_field_for_an_absent_key_changes_nothing(
    service: VaultService, remote: Remote
) -> None:
    before = remote.head()

    result = service.edit_note(
        OWNER, "ideas/Alpha.md", blob(remote, "ideas/Alpha.md"), fields={"missing": None}
    )

    assert result["changed"] is False
    assert remote.head() == before


# --- frontmatter: JSON in, YAML out ---------------------------------------------------------


# @spec VAULT-FM-003
@pytest.mark.parametrize(
    "value",
    [
        "plain text",
        "007",
        "true",
        "a: colon",
        "[[Gamma]]",
        3,
        -2,
        1.5,
        True,
        False,
        ["one", 2, False],
        [],
    ],
    ids=[
        "string",
        "numeric-looking-string",
        "boolean-looking-string",
        "string-with-colon",
        "wikilink-string",
        "int",
        "negative-int",
        "float",
        "true",
        "false",
        "mixed-list",
        "empty-list",
    ],
)
def test_field_value_is_written_as_the_corresponding_yaml_type(
    service: VaultService, remote: Remote, value: object
) -> None:
    service.edit_note(OWNER, "notes/Gamma.md", blob(remote, "notes/Gamma.md"), fields={"v": value})

    loaded = parsed_frontmatter(shown(remote, "notes/Gamma.md"))
    assert loaded["v"] == value
    assert type(loaded["v"]) is type(value)


# @spec VAULT-FM-003
def test_field_values_read_back_with_their_types(service: VaultService, remote: Remote) -> None:
    fields = {"s": "007", "n": 4, "b": True, "l": ["x", 1]}
    service.edit_note(OWNER, "notes/Gamma.md", blob(remote, "notes/Gamma.md"), fields=fields)

    frontmatter = service.read_note(OWNER, "notes/Gamma.md")["frontmatter"]

    for key, value in fields.items():
        assert frontmatter[key] == value
        assert type(frontmatter[key]) is type(value)


# --- frontmatter that does not parse --------------------------------------------------------


# @spec VAULT-FM-004
def test_read_note_returns_unparsed_frontmatter_as_marked_text(service: VaultService) -> None:
    result = service.read_note(OWNER, "ideas/Broken.md")

    assert result["frontmatter_unparsed"] is True
    assert isinstance(result["frontmatter"], str)
    assert "status: [unclosed" in result["frontmatter"]
    assert result["body"] == "# Broken\n"


# @spec VAULT-FM-004
def test_read_note_marks_parsed_frontmatter_as_parsed(service: VaultService) -> None:
    assert service.read_note(OWNER, "ideas/Alpha.md")["frontmatter_unparsed"] is False


# @spec VAULT-FM-004
def test_edit_note_fields_on_unparsed_frontmatter_fails(
    service: VaultService, remote: Remote
) -> None:
    before = remote.head()

    with pytest.raises(VaultError) as exc:
        service.edit_note(
            OWNER, "ideas/Broken.md", blob(remote, "ideas/Broken.md"), fields={"status": "x"}
        )

    assert exc.value.code == "frontmatter_unparsed"
    assert remote.head() == before


# @spec VAULT-FM-004
def test_archive_note_with_fields_on_unparsed_frontmatter_fails(
    service: VaultService, remote: Remote
) -> None:
    before = remote.head()

    with pytest.raises(VaultError) as exc:
        service.archive_note(
            OWNER, "ideas/Broken.md", blob(remote, "ideas/Broken.md"), {"status": "archived"}
        )

    assert exc.value.code == "frontmatter_unparsed"
    assert remote.head() == before
    assert shown(remote, "ideas/Broken.md") == SEED["ideas/Broken.md"]


# @spec VAULT-FM-004
def test_body_edit_on_unparsed_frontmatter_succeeds_and_keeps_it_verbatim(
    service: VaultService, remote: Remote
) -> None:
    """Only a call that would set the frontmatter is refused; the body stays editable."""
    service.edit_note(
        OWNER, "ideas/Broken.md", blob(remote, "ideas/Broken.md"), body="# Broken\n\nFixed.\n"
    )

    assert shown(remote, "ideas/Broken.md") == "---\nstatus: [unclosed\n---\n# Broken\n\nFixed.\n"
