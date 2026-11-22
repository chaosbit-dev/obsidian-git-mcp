"""Reading refusal lists from GitHub, against a mocked API (VAULT-ID-005, VAULT-ID-006)."""

from __future__ import annotations

import httpx2
import pytest

from obsidian_git_mcp.config import RefusalSource
from obsidian_git_mcp.refusal_sources import (
    GitHubRefusalReader,
    Location,
    RefusalSourceError,
    parse_location,
    static_credential,
)

REFUSAL_TOKEN = "github_pat_refusalsonly"
GUARDS = "\\bVoldemort\\b\n"


class GitHub:
    def __init__(self, status: int = 200, content: bytes = GUARDS.encode()) -> None:
        self.requests: list[httpx2.Request] = []
        self.status = status
        self.content = content

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, content=self.content)


def reader(github: GitHub, credential: str = REFUSAL_TOKEN) -> GitHubRefusalReader:
    return GitHubRefusalReader(
        static_credential(credential), transport=httpx2.MockTransport(github)
    )


# @spec VAULT-ID-005
def test_reads_the_file_at_the_ref_with_the_refusal_credential() -> None:
    github = GitHub()
    text = reader(github).read(RefusalSource("chaosbit-dev/holocron:guards.yaml@main"))
    assert text == GUARDS
    request = github.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/repos/chaosbit-dev/holocron/contents/guards.yaml"
    assert request.url.params["ref"] == "main"
    assert request.headers["Authorization"] == f"Bearer {REFUSAL_TOKEN}"
    assert request.headers["Accept"] == "application/vnd.github.raw+json"


# @spec VAULT-ID-005
def test_the_credential_is_asked_for_the_source_repository() -> None:
    asked: list[str] = []

    def credential(repository: str) -> str:
        asked.append(repository)
        return "minted-for-" + repository

    github = GitHub()
    GitHubRefusalReader(credential, transport=httpx2.MockTransport(github)).read(
        RefusalSource("chaosbit-dev/holocron:superseded.yaml", "yaml-mapping", "datapad/")
    )
    assert asked == ["chaosbit-dev/holocron"]
    assert github.requests[0].headers["Authorization"] == "Bearer minted-for-chaosbit-dev/holocron"
    assert "ref" not in github.requests[0].url.params


# @spec VAULT-ID-005
def test_the_reader_holds_no_vault_token_source() -> None:
    """Structural: the reader is built from its own credential alone, so it cannot use the
    vault's App even by mistake."""
    import inspect

    params = inspect.signature(GitHubRefusalReader).parameters
    assert next(iter(params)) == "credential"
    assert not any("token" in name for name in params)


# @spec VAULT-ID-005, VAULT-ID-006
@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_an_unreadable_source_raises_an_oserror_without_the_credential(status: int) -> None:
    github = GitHub(status=status, content=b'{"message": "nope"}')
    with pytest.raises(OSError) as raised:
        reader(github).read(RefusalSource("chaosbit-dev/holocron:guards.yaml@main"))
    assert isinstance(raised.value, RefusalSourceError)
    assert REFUSAL_TOKEN not in str(raised.value)
    assert "guards.yaml" in str(raised.value)


# @spec VAULT-ID-005
def test_a_network_failure_raises_an_oserror() -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down", request=request)

    with pytest.raises(OSError):
        GitHubRefusalReader(
            static_credential(REFUSAL_TOKEN), transport=httpx2.MockTransport(fail)
        ).read(RefusalSource("chaosbit-dev/holocron:guards.yaml"))


# @spec VAULT-ID-005
def test_a_malformed_location_raises_an_oserror_before_any_request() -> None:
    github = GitHub()
    with pytest.raises(OSError):
        reader(github).read(RefusalSource("guards.yaml"))
    assert github.requests == []


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("o/r:a.yaml", Location("o", "r", "a.yaml", None)),
        ("o/r:dir/a.yaml@main", Location("o", "r", "dir/a.yaml", "main")),
        ("o/r:/dir/a.yaml@v1.2", Location("o", "r", "dir/a.yaml", "v1.2")),
        ("o/r:a@b.txt@" + "f" * 40, Location("o", "r", "a@b.txt", "f" * 40)),
    ],
)
def test_parse_location(location: str, expected: Location) -> None:
    assert parse_location(location) == expected


@pytest.mark.parametrize(
    "location",
    ["guards.yaml", "o/r", "o/r:", "o/r:a.yaml@", "o:a.yaml", "o/r:../x.yaml", "o/r:a//b"],
)
def test_parse_location_rejects(location: str) -> None:
    with pytest.raises(ValueError):
        parse_location(location)
