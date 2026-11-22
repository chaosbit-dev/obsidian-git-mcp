"""Links (VAULT-LINK-*): which text is a link, and how a move, archive or delete treats links.

ideas/Alpha.md holds every recognised link form to Beta and every ignored form; notes/Gamma.md
links to Beta by folder path and back to Alpha with a relative markdown link. Single-form
probes are committed from the Mac as notes/Probe.md so each form is exercised on its own.

Where VAULT-LINK-006 leaves the exact spelling open (whether a rewritten `[[Beta.md]]` keeps its
extension, whether a path target carries `.md`), the tests accept both spellings and nothing else.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest

from tests.conftest import OWNER, SEED, git

if TYPE_CHECKING:
    from obsidian_git_mcp.service import VaultService
    from tests.conftest import Clock, Remote

ALPHA = SEED["ideas/Alpha.md"]
GAMMA = SEED["notes/Gamma.md"]

# The three Alpha lines that carry recognised links.
L_PLAIN = "Plain [[Beta]] and [[Beta#Design|the design]] and [[Beta#^blk]] and [[Beta.md]].\n"
L_EMBED = "Embed ![[Beta]] and folder [[ideas/Beta]].\n"
L_MD = "Markdown [b](Beta.md) and [b2](<Beta.md>) and [g](../notes/Gamma.md).\n"
# Everything after them: the ignored forms (fence, inline code, comment, escaped bracket).
IGNORED_TAIL = ALPHA[ALPHA.index("```\n[[Beta]] in a fence") :]
# Gamma's body line.
G_LINE = "See [[Alpha]] and [[ideas/Beta]] and [back](../ideas/Alpha.md).\n"

# Links to Beta in resolvable notes: eight in Alpha's body, one in Gamma.
BETA_LINKS = 9

for _line in (L_PLAIN, L_EMBED, L_MD):
    assert _line in ALPHA, _line
assert G_LINE in GAMMA


def outside(remote: Remote, clock: Clock, files: dict[str, str | None]) -> None:
    """Commit from the Mac, then move past the read freshness window."""
    remote.mac_commit(files)
    clock.advance(60)


def blob(remote: Remote, path: str) -> str:
    return git(remote.bare, "rev-parse", f"{remote.branch}:{path}").strip()


def show(remote: Remote, path: str) -> str:
    text = remote.show(path)
    assert text is not None, f"{path} missing on the remote"
    return text


def move(service: VaultService, remote: Remote, src: str, dst: str) -> dict[str, Any]:
    return service.move_note(OWNER, src, blob(remote, src), dst)


def rename_beta_to_bravo(service: VaultService, remote: Remote) -> dict[str, Any]:
    return move(service, remote, "ideas/Beta.md", "ideas/Bravo.md")


def replace_lines(text: str, pairs: dict[str, str]) -> str:
    for old, new in pairs.items():
        assert old in text, old
        text = text.replace(old, new)
    return text


def strings_in(value: Any) -> set[str]:
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


def mentions(listing: Any, path: str) -> bool:
    """True if any entry of a response listing names `path`, whatever the entry's shape."""
    return any(path in s for s in strings_in(listing))


PROBE = "notes/Probe.md"


def probe(form: str) -> str:
    return f"# Probe\n\n{form}\n"


# --- VAULT-LINK-001: recognised forms, one at a time ------------------------------------------

RECOGNISED = [
    pytest.param("[[Beta]]", "[[Bravo]]", id="name"),
    pytest.param("[[beta]]", "[[Bravo]]", id="name-other-case"),
    pytest.param("[[ideas/Beta]]", "[[Bravo]]", id="folder-prefix"),
    pytest.param("[[Beta#Design]]", "[[Bravo#Design]]", id="heading"),
    pytest.param("[[Beta#^blk]]", "[[Bravo#^blk]]", id="block"),
    pytest.param("[[Beta|shown]]", "[[Bravo|shown]]", id="alias"),
    pytest.param("[[Beta#Design|shown]]", "[[Bravo#Design|shown]]", id="heading-alias"),
    pytest.param("[[ideas/Beta#Design]]", "[[Bravo#Design]]", id="folder-heading"),
    pytest.param("![[Beta]]", "![[Bravo]]", id="embed"),
    pytest.param("![[Beta#Design]]", "![[Bravo#Design]]", id="embed-heading"),
    pytest.param("![[ideas/Beta#^blk|shown]]", "![[Bravo#^blk|shown]]", id="embed-full"),
    pytest.param("[t](../ideas/Beta.md)", "[t](../ideas/Bravo.md)", id="md-relative"),
    pytest.param("[t](<../ideas/Beta.md>)", "[t](<../ideas/Bravo.md>)", id="md-angle"),
]


# @spec VAULT-LINK-001, VAULT-LINK-004
@pytest.mark.parametrize(("form", "expected"), RECOGNISED)
def test_recognised_form_is_rewritten_on_rename(
    service: VaultService, remote: Remote, clock: Clock, form: str, expected: str
) -> None:
    outside(remote, clock, {PROBE: probe(form)})
    result = rename_beta_to_bravo(service, remote)
    assert show(remote, PROBE) == probe(expected)
    assert PROBE in result["also_changed"]


# @spec VAULT-LINK-001
def test_extension_form_is_rewritten_on_rename(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    outside(remote, clock, {PROBE: probe("[[Beta.md]]")})
    rename_beta_to_bravo(service, remote)
    assert show(remote, PROBE) in {probe("[[Bravo.md]]"), probe("[[Bravo]]")}


# @spec VAULT-LINK-001
def test_url_encoded_markdown_link_is_recognised(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    outside(
        remote,
        clock,
        {"ideas/Space Note.md": "# Space\n", PROBE: probe("[t](../ideas/Space%20Note.md)")},
    )
    move(service, remote, "ideas/Space Note.md", "ideas/Spaced.md")
    assert show(remote, PROBE) == probe("[t](../ideas/Spaced.md)")


# @spec VAULT-LINK-001
def test_angle_bracket_markdown_link_with_spaces_is_recognised(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    outside(
        remote,
        clock,
        {"ideas/Space Note.md": "# Space\n", PROBE: probe("[t](<../ideas/Space Note.md>)")},
    )
    move(service, remote, "ideas/Space Note.md", "ideas/Spaced.md")
    assert show(remote, PROBE) == probe("[t](<../ideas/Spaced.md>)")


# @spec VAULT-LINK-001, VAULT-LINK-004
def test_markdown_link_rewritten_to_a_name_with_spaces_still_resolves(
    service: VaultService, remote: Remote
) -> None:
    move(service, remote, "ideas/Beta.md", "ideas/Beta Two.md")
    alpha = show(remote, "ideas/Alpha.md")
    assert "[b](Beta%20Two.md)" in alpha or "[b](<Beta Two.md>)" in alpha
    assert "[b2](<Beta Two.md>)" in alpha


# @spec VAULT-LINK-001
def test_absolute_url_is_not_a_link_to_a_note(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    form = "[t](https://example.com/ideas/Beta.md)"
    outside(remote, clock, {PROBE: probe(form)})
    result = rename_beta_to_bravo(service, remote)
    assert show(remote, PROBE) == probe(form)
    assert PROBE not in result["also_changed"]


# --- VAULT-LINK-002: frontmatter string values ------------------------------------------------


# @spec VAULT-LINK-002
def test_frontmatter_string_value_link_is_rewritten(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    outside(remote, clock, {PROBE: '---\nup: "[[Beta]]"\nn: 3\n---\n# Probe\n'})
    rename_beta_to_bravo(service, remote)
    assert show(remote, PROBE) == '---\nup: "[[Bravo]]"\nn: 3\n---\n# Probe\n'


# @spec VAULT-LINK-002
def test_frontmatter_list_item_link_is_rewritten(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    outside(remote, clock, {PROBE: '---\nrefs: ["[[Beta#Design]]", plain]\n---\n# Probe\n'})
    rename_beta_to_bravo(service, remote)
    assert show(remote, PROBE) == '---\nrefs: ["[[Bravo#Design]]", plain]\n---\n# Probe\n'


# @spec VAULT-LINK-002, VAULT-LINK-004
def test_alpha_frontmatter_link_follows_a_gamma_rename_and_other_keys_keep_their_bytes(
    service: VaultService, remote: Remote
) -> None:
    move(service, remote, "notes/Gamma.md", "notes/Gemma.md")
    expected = replace_lines(
        ALPHA,
        {
            'related: "[[Gamma]]"\n': 'related: "[[Gemma]]"\n',
            "[g](../notes/Gamma.md)": "[g](../notes/Gemma.md)",
        },
    )
    assert show(remote, "ideas/Alpha.md") == expected


# --- VAULT-LINK-003: ignored forms ------------------------------------------------------------

IGNORED = [
    pytest.param("```\n[[Beta]]\n```", id="backtick-fence"),
    pytest.param("~~~\n[[Beta]]\n~~~", id="tilde-fence"),
    pytest.param("```md\n[t](../ideas/Beta.md)\n```", id="fence-md-link"),
    pytest.param("`[[Beta]]`", id="inline-code"),
    pytest.param("``[[Beta]]``", id="double-backtick-inline-code"),
    pytest.param("`[t](../ideas/Beta.md)`", id="inline-code-md-link"),
    pytest.param("<!-- [[Beta]] -->", id="html-comment"),
    pytest.param("<!--\n[[Beta]]\n-->", id="multiline-html-comment"),
    pytest.param("\\[[Beta]]", id="escaped-bracket"),
    pytest.param("\\![[Beta]]", id="escaped-embed"),
]


# @spec VAULT-LINK-003
@pytest.mark.parametrize("form", IGNORED)
def test_ignored_form_is_left_untouched_on_rename(
    service: VaultService, remote: Remote, clock: Clock, form: str
) -> None:
    outside(remote, clock, {PROBE: probe(form)})
    result = rename_beta_to_bravo(service, remote)
    assert show(remote, PROBE) == probe(form)
    assert PROBE not in result["also_changed"]


# @spec VAULT-LINK-003
@pytest.mark.parametrize("form", IGNORED)
def test_ignored_form_is_not_counted_as_a_dangling_link(
    service: VaultService, remote: Remote, clock: Clock, form: str
) -> None:
    outside(remote, clock, {PROBE: probe(form)})
    result = service.delete_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"))
    assert result["dangling_links"] == BETA_LINKS


# @spec VAULT-LINK-001, VAULT-LINK-003
def test_ignored_and_recognised_forms_in_one_note(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    before = "`[[Beta]]` then [[Beta]] then <!-- [[Beta]] --> then \\[[Beta]]"
    after = "`[[Beta]]` then [[Bravo]] then <!-- [[Beta]] --> then \\[[Beta]]"
    outside(remote, clock, {PROBE: probe(before)})
    rename_beta_to_bravo(service, remote)
    assert show(remote, PROBE) == probe(after)


# --- VAULT-LINK-004 / 006: rewriting links to the moved note ----------------------------------


# @spec VAULT-LINK-004, VAULT-LINK-006
def test_rename_rewrites_every_recognised_link_in_alpha_exactly(
    service: VaultService, remote: Remote
) -> None:
    rename_beta_to_bravo(service, remote)
    expected = replace_lines(
        ALPHA,
        {
            L_PLAIN: (
                "Plain [[Bravo]] and [[Bravo#Design|the design]] and [[Bravo#^blk]] "
                "and [[Bravo]].\n"
            ),
            L_EMBED: "Embed ![[Bravo]] and folder [[Bravo]].\n",
            L_MD: "Markdown [b](Bravo.md) and [b2](<Bravo.md>) and [g](../notes/Gamma.md).\n",
        },
    )
    # VAULT-LINK-006 leaves open whether `[[Beta.md]]` keeps its extension.
    assert show(remote, "ideas/Alpha.md").replace("[[Bravo.md]]", "[[Bravo]]") == expected


# @spec VAULT-LINK-003, VAULT-LINK-004
def test_rename_leaves_alpha_ignored_forms_byte_for_byte(
    service: VaultService, remote: Remote
) -> None:
    rename_beta_to_bravo(service, remote)
    assert show(remote, "ideas/Alpha.md").endswith(IGNORED_TAIL)


# @spec VAULT-LINK-004, VAULT-LINK-006
def test_rename_rewrites_gamma_folder_link_to_the_new_name(
    service: VaultService, remote: Remote
) -> None:
    rename_beta_to_bravo(service, remote)
    assert show(remote, "notes/Gamma.md") == replace_lines(GAMMA, {"[[ideas/Beta]]": "[[Bravo]]"})


# @spec VAULT-LINK-004
def test_rename_and_link_rewrites_are_one_commit(service: VaultService, remote: Remote) -> None:
    before = remote.commits()
    result = rename_beta_to_bravo(service, remote)
    assert remote.commits() == before + 1
    assert result["commit"] == remote.head()
    assert remote.show("ideas/Beta.md") is None
    assert remote.show("ideas/Bravo.md") == SEED["ideas/Beta.md"]
    assert "[[Bravo]]" in show(remote, "ideas/Alpha.md")
    assert "[[Bravo]]" in show(remote, "notes/Gamma.md")


# @spec VAULT-LINK-004
def test_folder_move_rewrites_only_links_that_would_break(
    service: VaultService, remote: Remote
) -> None:
    """Beta keeps its name and stays unique, so name links still resolve and stay as written."""
    move(service, remote, "ideas/Beta.md", "notes/Beta.md")
    expected = replace_lines(
        ALPHA,
        {
            L_EMBED: "Embed ![[Beta]] and folder [[Beta]].\n",
            L_MD: (
                "Markdown [b](../notes/Beta.md) and [b2](<../notes/Beta.md>) "
                "and [g](../notes/Gamma.md).\n"
            ),
        },
    )
    assert show(remote, "ideas/Alpha.md") == expected


# @spec VAULT-LINK-004
def test_folder_move_rewrites_gamma_folder_link(service: VaultService, remote: Remote) -> None:
    move(service, remote, "ideas/Beta.md", "notes/Beta.md")
    assert show(remote, "notes/Gamma.md") == replace_lines(GAMMA, {"[[ideas/Beta]]": "[[Beta]]"})


# @spec VAULT-LINK-004
def test_archive_rewrites_links_that_would_break(service: VaultService, remote: Remote) -> None:
    service.archive_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"))
    expected = replace_lines(
        ALPHA,
        {
            L_EMBED: "Embed ![[Beta]] and folder [[Beta]].\n",
            L_MD: (
                "Markdown [b](../archive/ideas/Beta.md) and [b2](<../archive/ideas/Beta.md>) "
                "and [g](../notes/Gamma.md).\n"
            ),
        },
    )
    assert show(remote, "ideas/Alpha.md") == expected
    assert show(remote, "notes/Gamma.md") == replace_lines(GAMMA, {"[[ideas/Beta]]": "[[Beta]]"})


# @spec VAULT-LINK-004
def test_archive_and_link_rewrites_are_one_commit(service: VaultService, remote: Remote) -> None:
    before = remote.commits()
    service.archive_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"))
    assert remote.commits() == before + 1
    assert remote.show("archive/ideas/Beta.md") == SEED["ideas/Beta.md"]
    assert "[[ideas/Beta]]" not in show(remote, "notes/Gamma.md")


# @spec VAULT-LINK-006
def test_rewrite_uses_a_path_where_the_new_name_is_not_unique(
    service: VaultService, remote: Remote
) -> None:
    """`Deep` resolves to the root Deep.md, so links to Beta moved to notes/sub/Deep.md must
    name the path, keeping embed marker, suffixes and alias."""
    move(service, remote, "ideas/Beta.md", "notes/sub/Deep.md")
    alpha = show(remote, "ideas/Alpha.md")
    target = r"notes/sub/Deep(?:\.md)?"
    for pattern in (
        rf"Plain \[\[{target}\]\] and ",
        rf"\[\[{target}#Design\|the design\]\]",
        rf"\[\[{target}#\^blk\]\]",
        rf"Embed !\[\[{target}\]\] and folder \[\[{target}\]\]\.",
    ):
        assert re.search(pattern, alpha), pattern
    assert "[[Deep" not in alpha
    assert "[b](../notes/sub/Deep.md)" in alpha


# @spec VAULT-LINK-006
def test_rewrite_keeps_embed_heading_block_and_alias(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    form = "![[Beta#Design|shown]] ![[Beta#^blk|b]] [[Beta#Notes]]"
    outside(remote, clock, {PROBE: probe(form)})
    rename_beta_to_bravo(service, remote)
    assert show(remote, PROBE) == probe(
        "![[Bravo#Design|shown]] ![[Bravo#^blk|b]] [[Bravo#Notes]]"
    )


# @spec VAULT-LINK-006
def test_rewrite_writes_a_name_when_the_old_link_was_a_path(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    outside(remote, clock, {PROBE: probe("[[ideas/Beta|shown]]")})
    move(service, remote, "ideas/Beta.md", "notes/deep/Unique.md")
    assert show(remote, PROBE) == probe("[[Unique|shown]]")


# --- VAULT-LINK-005: the moved note's own relative links --------------------------------------


# @spec VAULT-LINK-005
def test_moved_note_relative_links_are_fixed(service: VaultService, remote: Remote) -> None:
    move(service, remote, "notes/Gamma.md", "notes/ref/Gamma.md")
    assert show(remote, "notes/ref/Gamma.md") == replace_lines(
        GAMMA, {"[back](../ideas/Alpha.md)": "[back](../../ideas/Alpha.md)"}
    )


# @spec VAULT-LINK-004, VAULT-LINK-005
def test_moving_gamma_rewrites_alphas_relative_link_and_keeps_its_name_link(
    service: VaultService, remote: Remote
) -> None:
    move(service, remote, "notes/Gamma.md", "notes/ref/Gamma.md")
    assert show(remote, "ideas/Alpha.md") == replace_lines(
        ALPHA, {"[g](../notes/Gamma.md)": "[g](../notes/ref/Gamma.md)"}
    )


# @spec VAULT-LINK-005
def test_rename_in_place_leaves_moved_note_relative_links_alone(
    service: VaultService, remote: Remote
) -> None:
    move(service, remote, "notes/Gamma.md", "notes/Gemma.md")
    assert show(remote, "notes/Gemma.md") == GAMMA


# @spec VAULT-LINK-005
def test_moved_note_angle_bracket_and_sibling_links_are_fixed(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    outside(remote, clock, {PROBE: probe("[a](<../ideas/Alpha.md>) and [g](Gamma.md)")})
    move(service, remote, PROBE, "Probe.md")
    assert show(remote, "Probe.md") == probe("[a](<ideas/Alpha.md>) and [g](notes/Gamma.md)")


# @spec VAULT-LINK-005
def test_archived_note_relative_links_are_fixed(service: VaultService, remote: Remote) -> None:
    service.archive_note(OWNER, "notes/Gamma.md", blob(remote, "notes/Gamma.md"))
    assert show(remote, "archive/notes/Gamma.md") == replace_lines(
        GAMMA, {"[back](../ideas/Alpha.md)": "[back](../../ideas/Alpha.md)"}
    )


# --- VAULT-LINK-007: links already ambiguous ---------------------------------------------------


# @spec VAULT-LINK-007
def test_link_ambiguous_before_the_move_is_left_and_listed(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    uses = "notes/UsesTwin.md"
    outside(remote, clock, {uses: "See [[Twin]].\n"})
    result = move(service, remote, "ideas/Twin.md", "ideas/Twin2.md")
    assert show(remote, uses) == "See [[Twin]].\n"
    assert uses not in result["also_changed"]
    assert mentions(result["ambiguous_links"], uses)


# @spec VAULT-LINK-007
def test_only_the_ambiguous_link_is_left_when_a_note_has_both(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    uses = "notes/UsesTwin.md"
    outside(remote, clock, {uses: "See [[Twin]] and [[ideas/Twin]].\n"})
    result = move(service, remote, "ideas/Twin.md", "ideas/Twin2.md")
    assert show(remote, uses) == "See [[Twin]] and [[Twin2]].\n"
    assert uses in result["also_changed"]
    assert mentions(result["ambiguous_links"], uses)


# @spec VAULT-LINK-007
def test_no_ambiguous_links_reported_when_none_existed(
    service: VaultService, remote: Remote
) -> None:
    result = rename_beta_to_bravo(service, remote)
    assert list(result["ambiguous_links"]) == []


# --- VAULT-LINK-008: other notes changed --------------------------------------------------------


# @spec VAULT-LINK-008
def test_move_returns_every_other_note_it_changed(service: VaultService, remote: Remote) -> None:
    result = rename_beta_to_bravo(service, remote)
    assert sorted(result["also_changed"]) == ["ideas/Alpha.md", "notes/Gamma.md"]


# @spec VAULT-LINK-008
def test_move_without_inbound_links_changes_no_other_note(
    service: VaultService, remote: Remote
) -> None:
    result = move(service, remote, "Deep.md", "notes/Deeper.md")
    assert list(result["also_changed"]) == []
    assert result["path"] == "notes/Deeper.md"


# @spec VAULT-LINK-008
def test_archive_returns_every_other_note_it_changed(
    service: VaultService, remote: Remote
) -> None:
    result = service.archive_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"))
    assert sorted(result["also_changed"]) == ["ideas/Alpha.md", "notes/Gamma.md"]


# @spec VAULT-LINK-008, VAULT-LINK-009
def test_delete_returns_no_other_changed_notes(service: VaultService, remote: Remote) -> None:
    result = service.delete_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"))
    assert list(result["also_changed"]) == []


# --- VAULT-LINK-009: delete counts and leaves links ---------------------------------------------


# @spec VAULT-LINK-009
def test_delete_counts_links_to_the_deleted_note(service: VaultService, remote: Remote) -> None:
    result = service.delete_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"))
    assert result["dangling_links"] == BETA_LINKS


# @spec VAULT-LINK-009
def test_delete_leaves_links_to_the_deleted_note_unchanged(
    service: VaultService, remote: Remote
) -> None:
    service.delete_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"))
    assert show(remote, "ideas/Alpha.md") == ALPHA
    assert show(remote, "notes/Gamma.md") == GAMMA


# @spec VAULT-LINK-009
def test_delete_does_not_count_links_in_unresolvable_notes(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    outside(
        remote,
        clock,
        {".hidden/Links.md": "[[Beta]]\n", "trash/Old.md": "[[Beta]] and [[ideas/Beta]]\n"},
    )
    result = service.delete_note(OWNER, "ideas/Beta.md", blob(remote, "ideas/Beta.md"))
    assert result["dangling_links"] == BETA_LINKS


# @spec VAULT-LINK-009
def test_delete_of_an_unlinked_note_counts_zero(service: VaultService, remote: Remote) -> None:
    result = service.delete_note(OWNER, "Deep.md", blob(remote, "Deep.md"))
    assert result["dangling_links"] == 0
