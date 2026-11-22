"""The clone: lifecycle, serialisation, freshness, fetch failure, corruption, health, tokens.

Fetches and pushes are observed through FakeTokens.calls: the server mints a `read` token for
every fetch (the initial clone included) and a `write` token for every push (VAULT-ID-004).
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

from obsidian_git_mcp.errors import StartupError, VaultError
from obsidian_git_mcp.server import build_app
from obsidian_git_mcp.service import VaultService
from tests.conftest import HOST, OWNER, SEED, git, make_settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from obsidian_git_mcp.config import Settings
    from tests.conftest import Clock, FakeRefusals, FakeTokens, Remote

FRESH_PATH = "ideas/Fresh.md"
FRESH_TEXT = "# Fresh\n\nWritten on the Mac with the word zebra.\n"
BETA = "ideas/Beta.md"


def started(
    settings: Settings, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> VaultService:
    svc = VaultService(settings, tokens, refusals, clock=clock)
    svc.start()
    return svc


def workdir(settings: Settings) -> Path:
    return Path(settings.workdir)


def fetches(tokens: FakeTokens) -> int:
    return tokens.calls.count("read")


class RemoteOutage:
    """Makes the bare remote unreachable by renaming it, and brings it back."""

    def __init__(self, remote: Remote) -> None:
        self.remote = remote
        self.away = remote.bare.with_name(remote.bare.name + ".away")

    def __enter__(self) -> RemoteOutage:
        self.remote.bare.rename(self.away)
        return self

    def __exit__(self, *exc: object) -> None:
        self.restore()

    def restore(self) -> None:
        if self.away.exists():
            self.away.rename(self.remote.bare)


def issued_tokens(tokens: FakeTokens) -> list[str]:
    """The exact token strings FakeTokens handed out, in order."""
    return [f"tok-{perm}-{i}" for i, perm in enumerate(tokens.calls, start=1)]


def _paths(result: dict[str, Any]) -> list[str]:
    return [n["path"] for n in result["notes"]]


def regular_files(root: Path) -> Iterator[Path]:
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            p = Path(dirpath) / name
            if p.is_file() and not p.is_symlink():
                yield p


# --- VAULT-CLONE-001: start wipes the workdir and makes a full clone ----------------------


# @spec VAULT-CLONE-001
def test_start_deletes_stray_files_in_workdir(
    remote: Remote, tmp_path: Path, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> None:
    settings = make_settings(remote, tmp_path / "work")
    workdir(settings).mkdir()
    (workdir(settings) / "stray.txt").write_text("left over from a previous pod\n")

    started(settings, tokens, refusals, clock)

    assert not (workdir(settings) / "stray.txt").exists()


# @spec VAULT-CLONE-001
def test_start_replaces_an_existing_clone_rather_than_reusing_it(
    remote: Remote, tmp_path: Path, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> None:
    """A leftover clone with a local-only commit must not survive: the start is a fresh clone."""
    settings = make_settings(remote, tmp_path / "work")
    git(tmp_path, "clone", "-q", str(remote.bare), str(workdir(settings)))
    git(workdir(settings), "config", "user.name", "Old Pod")
    git(workdir(settings), "config", "user.email", "old@example.com")
    (workdir(settings) / "ideas" / "Orphan.md").write_text("# Orphan\n")
    git(workdir(settings), "add", "-A")
    git(workdir(settings), "commit", "-q", "-m", "local only")

    started(settings, tokens, refusals, clock)

    subjects = git(workdir(settings), "log", "--all", "--format=%s").splitlines()
    assert "local only" not in subjects
    assert not (workdir(settings) / "ideas" / "Orphan.md").exists()


# @spec VAULT-CLONE-001
def test_start_clone_is_at_the_remote_tip(
    remote: Remote, tmp_path: Path, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> None:
    remote.mac_commit({FRESH_PATH: FRESH_TEXT})
    settings = make_settings(remote, tmp_path / "work")

    started(settings, tokens, refusals, clock)

    assert git(workdir(settings), "rev-parse", "HEAD").strip() == remote.head()


# @spec VAULT-CLONE-001
def test_start_clone_is_not_shallow(
    remote: Remote, tmp_path: Path, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> None:
    remote.mac_commit({FRESH_PATH: FRESH_TEXT})
    remote.mac_commit({BETA: "# Beta\n\nRewritten.\n"})
    settings = make_settings(remote, tmp_path / "work")

    started(settings, tokens, refusals, clock)

    assert git(workdir(settings), "rev-parse", "--is-shallow-repository").strip() == "false"
    assert int(git(workdir(settings), "rev-list", "--count", "HEAD").strip()) == remote.commits()


# @spec VAULT-CLONE-001
def test_start_clones_the_configured_branch(
    remote: Remote, tmp_path: Path, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> None:
    git(remote.mac, "checkout", "-q", "-b", "vault")
    (remote.mac / "ideas" / "OnVault.md").write_text("# Only on the vault branch\n")
    git(remote.mac, "add", "-A")
    git(remote.mac, "commit", "-q", "-m", "vault branch")
    git(remote.mac, "push", "-q", "origin", "vault")
    vault_tip = git(remote.bare, "rev-parse", "vault").strip()
    settings = make_settings(remote, tmp_path / "work", branch="vault")

    started(settings, tokens, refusals, clock)

    assert git(workdir(settings), "rev-parse", "HEAD").strip() == vault_tip
    assert (workdir(settings) / "ideas" / "OnVault.md").exists()


# @spec VAULT-CLONE-001
def test_a_call_right_after_start_is_answered_from_the_clone(
    remote: Remote, tmp_path: Path, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> None:
    """start() returns only once the clone is ready to serve."""
    settings = make_settings(remote, tmp_path / "work")
    svc = started(settings, tokens, refusals, clock)

    result = svc.read_note(OWNER, BETA)

    assert result["body"] == SEED[BETA]
    assert result["commit"] == remote.head()


# --- VAULT-CLONE-002: a failed initial clone stops the server --------------------------------


# @spec VAULT-CLONE-002
def test_start_fails_when_the_remote_does_not_exist(
    remote: Remote, tmp_path: Path, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> None:
    settings = make_settings(
        remote, tmp_path / "work", remote_url=str(tmp_path / "no-such-remote.git")
    )
    svc = VaultService(settings, tokens, refusals, clock=clock)

    with pytest.raises(StartupError):
        svc.start()


# @spec VAULT-CLONE-002
def test_start_fails_when_the_configured_branch_does_not_exist(
    remote: Remote, tmp_path: Path, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> None:
    settings = make_settings(remote, tmp_path / "work", branch="no-such-branch")
    svc = VaultService(settings, tokens, refusals, clock=clock)

    with pytest.raises(StartupError):
        svc.start()


# --- VAULT-CLONE-003: one call at a time ---------------------------------------------------


def _install_blocking_pre_receive(remote: Remote, gate: Path) -> tuple[Path, Path]:
    """Make the next push hang inside git until `release` exists. Returns (entered, release)."""
    gate.mkdir()
    entered, release = gate / "entered", gate / "release"
    hook = remote.bare / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"
        f'touch "{entered}"\n'
        f'while [ ! -f "{release}" ]; do sleep 0.05; done\n'
    )
    hook.chmod(0o755)
    return entered, release


def _wait_for(path: Path, guard: Any, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if guard.done():
            guard.result()  # surface the call's own failure
            pytest.fail("the write finished without reaching its push")
        if time.monotonic() > deadline:
            pytest.fail("the write never reached its push")
        time.sleep(0.02)


# @spec VAULT-CLONE-003
def test_a_read_waits_while_a_write_is_inside_its_push(
    service: VaultService, remote: Remote, tmp_path: Path
) -> None:
    """The lock spans the write's fetch to its push, so a read cannot run in between."""
    entered, release = _install_blocking_pre_receive(remote, tmp_path / "gate")
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            write = pool.submit(service.create_note, OWNER, "ideas/Locked.md", "# Locked\n")
            _wait_for(entered, write)
            read = pool.submit(service.read_note, OWNER, BETA)
            time.sleep(0.5)
            assert not read.done(), "read ran while a write held the clone"
        finally:
            release.touch()
        write_result = write.result(timeout=30)
        read_result = read.result(timeout=30)

    assert read_result["commit"] == write_result["commit"] == remote.head()


# @spec VAULT-CLONE-003
def test_a_second_write_waits_while_the_first_is_inside_its_push(
    service: VaultService, remote: Remote, tmp_path: Path
) -> None:
    entered, release = _install_blocking_pre_receive(remote, tmp_path / "gate")
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first = pool.submit(service.create_note, OWNER, "ideas/First.md", "# First\n")
            _wait_for(entered, first)
            second = pool.submit(service.append_to_note, OWNER, BETA, "Appended second.\n")
            time.sleep(0.5)
            assert not second.done(), "second write ran while the first held the clone"
        finally:
            release.touch()
        first.result(timeout=30)
        second.result(timeout=30)

    assert remote.commits() == 3
    assert remote.show("ideas/First.md") == "# First\n"
    beta = remote.show(BETA)
    assert beta is not None and "Appended second." in beta


# --- VAULT-CLONE-004 / 005: fetch and reset at the start of a call ----------------------------


# @spec VAULT-CLONE-004
@pytest.mark.parametrize(
    "sees_fresh",
    [
        pytest.param(lambda s: s.read_note(OWNER, FRESH_PATH)["body"] == FRESH_TEXT, id="read"),
        pytest.param(lambda s: s.search_notes(OWNER, query="zebra")["total"] == 1, id="search"),
        pytest.param(
            lambda s: FRESH_PATH in _paths(s.list_notes(OWNER, folder="ideas")), id="list"
        ),
    ],
)
def test_read_after_the_window_sees_a_mac_commit(
    service: VaultService,
    remote: Remote,
    clock: Clock,
    sees_fresh: Callable[[VaultService], bool],
) -> None:
    remote.mac_commit({FRESH_PATH: FRESH_TEXT})
    clock.advance(16)

    assert sees_fresh(service)


# @spec VAULT-CLONE-004
def test_read_after_the_window_answers_from_the_new_tip(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    tip = remote.mac_commit({FRESH_PATH: FRESH_TEXT})
    clock.advance(16)

    assert service.read_note(OWNER, BETA)["commit"] == tip


# @spec VAULT-CLONE-004
def test_write_inside_the_window_still_fetches_first(
    service: VaultService, remote: Remote, tokens: FakeTokens
) -> None:
    """A write never trusts the freshness window: it builds on the Mac's commit, not over it."""
    before = fetches(tokens)
    mac_tip = remote.mac_commit({FRESH_PATH: FRESH_TEXT})

    service.append_to_note(OWNER, BETA, "From the phone.\n")

    assert fetches(tokens) == before + 1
    assert remote.show(FRESH_PATH) == FRESH_TEXT
    assert git(remote.bare, "rev-parse", "main~1").strip() == mac_tip


# @spec VAULT-CLONE-004
def test_read_resets_away_local_edits_in_the_clone(
    service: VaultService, settings: Settings, clock: Clock
) -> None:
    (workdir(settings) / BETA).write_text("# Beta\n\nScribbled on in the clone.\n")
    clock.advance(16)

    assert service.read_note(OWNER, BETA)["body"] == SEED[BETA]


# @spec VAULT-CLONE-004
def test_read_follows_a_force_pushed_rewrite(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    remote.mac_commit({FRESH_PATH: FRESH_TEXT})
    clock.advance(16)
    service.read_note(OWNER, BETA)
    remote.force_push_rewrite()
    clock.advance(16)

    assert service.read_note(OWNER, BETA)["commit"] == remote.head()


# @spec VAULT-CLONE-004
def test_write_after_a_force_push_builds_on_the_rewritten_tip(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    remote.mac_commit({FRESH_PATH: FRESH_TEXT})
    clock.advance(16)
    service.read_note(OWNER, BETA)
    remote.force_push_rewrite()
    rewritten = remote.head()

    service.append_to_note(OWNER, BETA, "After the rewrite.\n")

    assert git(remote.bare, "rev-parse", "main~1").strip() == rewritten


# @spec VAULT-CLONE-004, VAULT-CLONE-005
def test_read_inside_the_default_window_does_not_fetch(
    service: VaultService, tokens: FakeTokens, clock: Clock
) -> None:
    clock.advance(100)
    service.read_note(OWNER, BETA)
    before = fetches(tokens)
    clock.advance(14)

    service.read_note(OWNER, BETA)

    assert fetches(tokens) == before


# @spec VAULT-CLONE-005
def test_read_past_the_default_window_fetches(
    service: VaultService, tokens: FakeTokens, clock: Clock
) -> None:
    clock.advance(100)
    service.read_note(OWNER, BETA)
    before = fetches(tokens)
    clock.advance(15.5)

    service.read_note(OWNER, BETA)

    assert fetches(tokens) == before + 1


# @spec VAULT-CLONE-005
def test_configured_window_replaces_the_default(
    remote: Remote, tmp_path: Path, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> None:
    settings = make_settings(remote, tmp_path / "work", freshness_seconds=60.0)
    svc = started(settings, tokens, refusals, clock)
    clock.advance(100)
    svc.read_note(OWNER, BETA)
    before = fetches(tokens)

    clock.advance(30)
    svc.read_note(OWNER, BETA)
    assert fetches(tokens) == before

    clock.advance(31)
    svc.read_note(OWNER, BETA)
    assert fetches(tokens) == before + 1


# @spec VAULT-CLONE-005
def test_a_zero_window_fetches_on_every_read(
    remote: Remote, tmp_path: Path, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> None:
    settings = make_settings(remote, tmp_path / "work", freshness_seconds=0.0)
    svc = started(settings, tokens, refusals, clock)
    before = fetches(tokens)

    svc.read_note(OWNER, BETA)
    svc.read_note(OWNER, BETA)

    assert fetches(tokens) == before + 2


# --- VAULT-CLONE-006: a failed fetch fails the call ------------------------------------------


# @spec VAULT-CLONE-006
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda s: s.read_note(OWNER, BETA), id="read_note"),
        pytest.param(lambda s: s.search_notes(OWNER, query="Beta"), id="search_notes"),
        pytest.param(lambda s: s.list_notes(OWNER), id="list_notes"),
    ],
)
def test_read_fails_with_fetch_failed_when_the_remote_is_unreachable(
    service: VaultService, remote: Remote, clock: Clock, call: Callable[[VaultService], Any]
) -> None:
    clock.advance(16)
    with RemoteOutage(remote), pytest.raises(VaultError) as err:
        call(service)

    assert err.value.code == "fetch_failed"


# @spec VAULT-CLONE-006
def test_write_fails_with_fetch_failed_and_pushes_nothing(
    service: VaultService, remote: Remote, tokens: FakeTokens
) -> None:
    before = remote.head()
    with RemoteOutage(remote), pytest.raises(VaultError) as err:
        service.create_note(OWNER, "ideas/Offline.md", "# Offline\n")

    assert err.value.code == "fetch_failed"
    assert remote.head() == before
    assert remote.show("ideas/Offline.md") is None
    assert "write" not in tokens.calls


# @spec VAULT-CLONE-006
def test_fetch_failed_carries_no_note_content(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    """Nothing from the stale clone leaks out through the error either."""
    clock.advance(16)
    with RemoteOutage(remote), pytest.raises(VaultError) as err:
        service.read_note(OWNER, BETA)

    assert "Design text." not in repr(err.value.detail)
    assert "body" not in err.value.detail


# @spec VAULT-CLONE-006
def test_calls_succeed_again_once_the_remote_is_back(
    service: VaultService, remote: Remote, clock: Clock
) -> None:
    clock.advance(16)
    outage = RemoteOutage(remote)
    with outage, pytest.raises(VaultError):
        service.read_note(OWNER, BETA)
    tip = remote.mac_commit({FRESH_PATH: FRESH_TEXT})

    result = service.read_note(OWNER, FRESH_PATH)

    assert result["commit"] == tip


# --- VAULT-CLONE-007: a corrupt clone is re-cloned ------------------------------------------


def _corrupt_index(settings: Settings) -> None:
    index = workdir(settings) / ".git" / "index"
    index.unlink()
    index.write_bytes(b"DIRC this is not an index")


def _drop_all_objects(settings: Settings) -> None:
    # unlink, never write in place: a local clone hardlinks objects shared with the remote
    for p in list(regular_files(workdir(settings) / ".git" / "objects")):
        p.unlink()


# @spec VAULT-CLONE-007
@pytest.mark.parametrize(
    "corrupt",
    [pytest.param(_corrupt_index, id="index"), pytest.param(_drop_all_objects, id="objects")],
)
def test_read_on_a_corrupt_clone_reclones_and_answers(
    service: VaultService,
    remote: Remote,
    settings: Settings,
    clock: Clock,
    corrupt: Callable[[Settings], None],
) -> None:
    corrupt(settings)
    clock.advance(16)

    result = service.read_note(OWNER, BETA)

    assert result["body"] == SEED[BETA]
    assert result["commit"] == remote.head()


# @spec VAULT-CLONE-007
def test_reclone_leaves_a_sound_repository(
    service: VaultService, settings: Settings, clock: Clock
) -> None:
    _drop_all_objects(settings)
    clock.advance(16)

    service.read_note(OWNER, BETA)

    git(workdir(settings), "fsck", "--no-dangling")  # raises on a broken repository


# @spec VAULT-CLONE-007
def test_write_on_a_corrupt_clone_reclones_and_completes(
    service: VaultService, remote: Remote, settings: Settings
) -> None:
    _drop_all_objects(settings)

    result = service.create_note(OWNER, "ideas/AfterRepair.md", "# After repair\n")

    assert remote.show("ideas/AfterRepair.md") == "# After repair\n"
    assert result["commit"] == remote.head()


# --- VAULT-CLONE-008: every response names its commit ---------------------------------------


# @spec VAULT-CLONE-008
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda s: s.read_note(OWNER, BETA), id="read_note"),
        pytest.param(lambda s: s.search_notes(OWNER, query="Beta"), id="search_notes"),
        pytest.param(lambda s: s.list_notes(OWNER), id="list_notes"),
    ],
)
def test_read_response_names_the_commit_it_was_answered_from(
    service: VaultService, remote: Remote, clock: Clock, call: Callable[[VaultService], Any]
) -> None:
    tip = remote.mac_commit({FRESH_PATH: FRESH_TEXT})
    clock.advance(16)

    assert call(service)["commit"] == tip


# @spec VAULT-CLONE-008
def test_every_page_names_its_commit(service: VaultService, remote: Remote, clock: Clock) -> None:
    first = service.list_notes(OWNER, limit=2, page=1)
    tip = remote.mac_commit({FRESH_PATH: FRESH_TEXT})
    clock.advance(16)
    second = service.list_notes(OWNER, limit=2, page=2)

    assert first["commit"] != second["commit"]
    assert second["commit"] == tip


def _rev(s: VaultService, note: str) -> str:
    revision: str = s.read_note(OWNER, note)["revision"]
    return revision


# @spec VAULT-CLONE-008
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda s: s.create_note(OWNER, "ideas/New.md", "# New\n"), id="create"),
        pytest.param(lambda s: s.append_to_note(OWNER, BETA, "More.\n"), id="append"),
        pytest.param(
            lambda s: s.edit_note(OWNER, "notes/Twin.md", _rev(s, "notes/Twin.md"), body="# E\n"),
            id="edit",
        ),
        pytest.param(
            lambda s: s.move_note(
                OWNER, "ideas/Twin.md", _rev(s, "ideas/Twin.md"), "ideas/Triplet.md"
            ),
            id="move",
        ),
        pytest.param(
            lambda s: s.delete_note(OWNER, "notes/Twin.md", _rev(s, "notes/Twin.md")),
            id="delete",
        ),
        pytest.param(
            lambda s: s.archive_note(OWNER, "ideas/Twin.md", _rev(s, "ideas/Twin.md")),
            id="archive",
        ),
    ],
)
def test_write_response_names_the_commit_it_made(
    service: VaultService, remote: Remote, call: Callable[[VaultService], Any]
) -> None:
    result = call(service)

    assert result["changed"] is True
    assert result["commit"] == remote.head()
    assert remote.commits() == 2


# @spec VAULT-CLONE-008
def test_no_op_write_names_the_commit_it_was_answered_from(
    service: VaultService, remote: Remote
) -> None:
    """The service docstring drops `commit` on a no-op (no commit was made), but the spec still
    requires the response to say which commit it was answered from: `answered_from` here."""
    result = service.edit_note(OWNER, BETA, _rev(service, BETA), body=SEED[BETA])

    assert result["changed"] is False
    assert result["answered_from"] == remote.head()
    assert remote.commits() == 1


# --- VAULT-CLONE-009: health follows fetch failure ------------------------------------------


@pytest.fixture
def short_unhealthy(
    remote: Remote, tmp_path: Path, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> tuple[Settings, VaultService]:
    settings = make_settings(remote, tmp_path / "work", unhealthy_after_seconds=60.0)
    return settings, started(settings, tokens, refusals, clock)


def _failed_read(svc: VaultService) -> None:
    with pytest.raises(VaultError) as err:
        svc.read_note(OWNER, BETA)
    assert err.value.code == "fetch_failed"


# @spec VAULT-CLONE-009
def test_healthy_after_start(short_unhealthy: tuple[Settings, VaultService]) -> None:
    _, svc = short_unhealthy

    assert svc.healthy() is True


# @spec VAULT-CLONE-009
def test_an_idle_server_stays_healthy(
    short_unhealthy: tuple[Settings, VaultService], clock: Clock
) -> None:
    """Not fetching because nobody called is not failing to fetch."""
    _, svc = short_unhealthy
    clock.advance(10_000)

    assert svc.healthy() is True


# @spec VAULT-CLONE-009
def test_failing_for_less_than_the_period_stays_healthy(
    short_unhealthy: tuple[Settings, VaultService], remote: Remote, clock: Clock
) -> None:
    _, svc = short_unhealthy
    with RemoteOutage(remote):
        clock.advance(16)
        _failed_read(svc)
        clock.advance(30)
        _failed_read(svc)

    assert svc.healthy() is True


# @spec VAULT-CLONE-009
def test_failing_for_longer_than_the_period_is_unhealthy(
    short_unhealthy: tuple[Settings, VaultService], remote: Remote, clock: Clock
) -> None:
    _, svc = short_unhealthy
    with RemoteOutage(remote):
        clock.advance(16)
        _failed_read(svc)
        clock.advance(61)
        _failed_read(svc)

    assert svc.healthy() is False


# @spec VAULT-CLONE-009
def test_a_successful_fetch_restores_health(
    short_unhealthy: tuple[Settings, VaultService], remote: Remote, clock: Clock
) -> None:
    _, svc = short_unhealthy
    with RemoteOutage(remote):
        clock.advance(16)
        _failed_read(svc)
        clock.advance(61)
        _failed_read(svc)
    assert svc.healthy() is False

    svc.read_note(OWNER, BETA)

    assert svc.healthy() is True


async def _healthz(settings: Settings, svc: VaultService) -> httpx2.Response:
    app = build_app(settings, svc)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=f"http://{HOST}"
    ) as client:
        return await client.get("/healthz")


# @spec VAULT-CLONE-009
async def test_healthz_answers_503_when_unhealthy(
    short_unhealthy: tuple[Settings, VaultService], remote: Remote, clock: Clock
) -> None:
    settings, svc = short_unhealthy
    with RemoteOutage(remote):
        clock.advance(16)
        _failed_read(svc)
        clock.advance(61)
        _failed_read(svc)

    response = await _healthz(settings, svc)

    assert response.status_code == 503


# @spec VAULT-CLONE-009
async def test_healthz_answers_200_while_failing_for_less_than_the_period(
    short_unhealthy: tuple[Settings, VaultService], remote: Remote, clock: Clock
) -> None:
    settings, svc = short_unhealthy
    with RemoteOutage(remote):
        clock.advance(16)
        _failed_read(svc)

    response = await _healthz(settings, svc)

    assert response.status_code == 200


# --- VAULT-ID-004: a fresh token per operation, scoped to it --------------------------------


# @spec VAULT-ID-004
def test_initial_clone_mints_one_read_token(service: VaultService, tokens: FakeTokens) -> None:
    assert tokens.calls == ["read"]


# @spec VAULT-ID-004
def test_a_fetching_read_mints_a_read_token(
    service: VaultService, tokens: FakeTokens, clock: Clock
) -> None:
    clock.advance(16)

    service.read_note(OWNER, BETA)

    assert tokens.calls == ["read", "read"]


# @spec VAULT-ID-004
def test_a_write_mints_read_for_its_fetch_then_write_for_its_push(
    service: VaultService, tokens: FakeTokens
) -> None:
    service.append_to_note(OWNER, BETA, "One.\n")

    assert tokens.calls == ["read", "read", "write"]


# @spec VAULT-ID-004
def test_each_operation_gets_its_own_token(service: VaultService, tokens: FakeTokens) -> None:
    """Tokens are minted per operation, never cached and reused across calls."""
    service.append_to_note(OWNER, BETA, "One.\n")
    service.append_to_note(OWNER, BETA, "Two.\n")

    assert tokens.calls == ["read", "read", "write", "read", "write"]


# @spec VAULT-ID-004
def test_a_write_that_changes_nothing_mints_no_write_token(
    service: VaultService, tokens: FakeTokens
) -> None:
    service.edit_note(OWNER, BETA, _rev(service, BETA), body=SEED[BETA])

    assert "write" not in tokens.calls


# --- VAULT-ID-006: tokens stay out of files, logs and responses -----------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME and XDG dirs into tmp so any credential file git writes is scanned too."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    return home


def _exercise(svc: VaultService, clock: Clock) -> list[Any]:
    """A start has already happened; add a fetching read and a pushing write."""
    clock.advance(16)
    results: list[Any] = [svc.read_note(OWNER, BETA)]
    results.append(svc.append_to_note(OWNER, BETA, "Token check.\n"))
    return results


# @spec VAULT-ID-006
def test_no_token_is_written_to_any_file(
    isolated_home: Path,
    remote: Remote,
    tmp_path: Path,
    tokens: FakeTokens,
    refusals: FakeRefusals,
    clock: Clock,
) -> None:
    settings = make_settings(remote, tmp_path / "work")
    svc = started(settings, tokens, refusals, clock)
    _exercise(svc, clock)
    issued = issued_tokens(tokens)
    assert "write" in tokens.calls  # the push really happened

    leaks = [
        str(p.relative_to(tmp_path))
        for p in regular_files(tmp_path)
        for tok in issued
        if tok.encode() in p.read_bytes()
    ]
    assert leaks == []


# @spec VAULT-ID-006
def test_no_token_is_in_the_clone_remote_url(service: VaultService, settings: Settings) -> None:
    url = git(workdir(settings), "config", "--get", "remote.origin.url").strip()

    assert "tok-" not in url


# @spec VAULT-ID-006
def test_no_token_appears_in_logs_or_output(
    isolated_home: Path,
    remote: Remote,
    tmp_path: Path,
    tokens: FakeTokens,
    refusals: FakeRefusals,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    settings = make_settings(remote, tmp_path / "work")
    svc = started(settings, tokens, refusals, clock)
    _exercise(svc, clock)
    out, errout = capfd.readouterr()
    logged = "\n".join(r.getMessage() + repr(r.args) + repr(r.__dict__) for r in caplog.records)

    for tok in issued_tokens(tokens):
        assert tok not in logged
        assert tok not in out
        assert tok not in errout


# @spec VAULT-ID-006
def test_no_token_appears_in_a_response(
    service: VaultService, tokens: FakeTokens, clock: Clock
) -> None:
    results = _exercise(service, clock)

    for tok in issued_tokens(tokens):
        assert all(tok not in repr(r) for r in results)


# @spec VAULT-ID-006
def test_no_token_appears_in_a_fetch_failure(
    service: VaultService,
    remote: Remote,
    tokens: FakeTokens,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """git's own error text names the remote URL; a token must not ride along in it."""
    caplog.set_level(logging.DEBUG)
    clock.advance(16)
    with RemoteOutage(remote), pytest.raises(VaultError) as err:
        service.read_note(OWNER, BETA)
    logged = "\n".join(r.getMessage() for r in caplog.records)

    for tok in issued_tokens(tokens):
        assert tok not in repr(err.value.detail)
        assert tok not in str(err.value)
        assert tok not in logged
