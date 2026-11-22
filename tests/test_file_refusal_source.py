"""Refusal sources read from the local filesystem: `file:/absolute/path`.

A deployment can supply a list no GitHub repository holds (a ConfigMap, say). It needs no
credential, and it fails the same way a GitHub source does, so a missing file refuses writes
(VAULT-REFUSE-010) rather than emptying the list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from obsidian_git_mcp.config import RefusalSource
from obsidian_git_mcp.refusal_sources import (
    MAX_BYTES,
    GitHubRefusalReader,
    RefusalSourceError,
    is_file_location,
    static_credential,
)

if TYPE_CHECKING:
    from pathlib import Path


def reader() -> GitHubRefusalReader:
    def refuse(_repository: str) -> str:
        raise AssertionError("a file source must not ask for a credential")

    return GitHubRefusalReader(refuse)


# @spec VAULT-REFUSE-002
def test_reads_a_file_location(tmp_path: Path) -> None:
    pins = tmp_path / "pins.txt"
    pins.write_text("README.md\nideas/README.md\n")
    assert reader().read(RefusalSource(f"file:{pins}")) == "README.md\nideas/README.md\n"


def test_a_file_location_needs_no_credential(tmp_path: Path) -> None:
    pins = tmp_path / "pins.txt"
    pins.write_text("README.md\n")
    # `reader()` raises if asked for a credential.
    reader().read(RefusalSource(f"file:{pins}"))


@pytest.mark.parametrize("location", ["file:relative/pins.txt", "file:", "file:~/pins.txt"])
def test_a_file_location_must_be_absolute(location: str) -> None:
    with pytest.raises(RefusalSourceError):
        reader().read(RefusalSource(location))


def test_a_missing_file_is_a_source_error(tmp_path: Path) -> None:
    with pytest.raises(RefusalSourceError) as caught:
        reader().read(RefusalSource(f"file:{tmp_path / 'absent.txt'}"))
    assert isinstance(caught.value, OSError)


def test_an_oversized_file_is_a_source_error(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (MAX_BYTES + 1))
    with pytest.raises(RefusalSourceError):
        reader().read(RefusalSource(f"file:{big}"))


def test_a_non_utf8_file_is_a_source_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(RefusalSourceError):
        reader().read(RefusalSource(f"file:{bad}"))


def test_the_error_names_the_location_not_the_content(tmp_path: Path) -> None:
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"secret-ish\xff")
    with pytest.raises(RefusalSourceError) as caught:
        reader().read(RefusalSource(f"file:{bad}"))
    assert "secret-ish" not in str(caught.value)
    assert str(bad) in str(caught.value)


def test_is_file_location() -> None:
    assert is_file_location("file:/etc/pins.txt")
    assert not is_file_location("chaosbit-dev/holocron:guards.yaml@main")


def test_github_locations_still_use_the_credential(tmp_path: Path) -> None:
    # A GitHub location is unaffected: it still goes to the API with the refusal credential.
    import httpx2

    seen: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx2.Response(200, text="x\n")

    r = GitHubRefusalReader(
        static_credential("tok-refusal"), transport=httpx2.MockTransport(handle)
    )
    assert r.read(RefusalSource("chaosbit-dev/holocron:guards.yaml@main")) == "x\n"
    assert seen and "tok-refusal" in seen[0]
