"""Loading settings from the environment and a YAML file, and main()'s refusal to start."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from obsidian_git_mcp import server, settings_env
from obsidian_git_mcp.config import Identity, RefusalSource
from obsidian_git_mcp.errors import StartupError

if TYPE_CHECKING:
    from pathlib import Path

P = settings_env.ENV_PREFIX


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "app.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


@pytest.fixture
def config_file(tmp_path: Path, key_file: Path) -> Path:
    token = tmp_path / "refusal-token"
    token.write_text("github_pat_x\n")
    path = tmp_path / "config.yaml"
    path.write_text(
        "remote_url: https://github.com/chaosbit-dev/datapad.git\n"
        "allowed_hosts: [vault.example.com]\n"
        "access_team_domain: team.cloudflareaccess.com\n"
        "access_audience: aud\n"
        "identities:\n"
        "  - {email: o@example.com, mode: write, author_name: O, author_email: o@example.com}\n"
        "archive_folder: archive\n"
        "pinned_sources:\n"
        "  - {location: 'chaosbit-dev/holocron:superseded.yaml@main',"
        " extraction: yaml-mapping, prefix: datapad/}\n"
        "guard_source: {location: 'chaosbit-dev/holocron:guards.yaml@main'}\n"
        'github_app_id: "1"\n'
        "github_installation_id: 2\n"
        f"github_app_private_key_file: {key_file}\n"
        f"refusal_token_file: {token}\n"
    )
    return path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        if name.startswith(P):
            monkeypatch.delenv(name)


def test_loads_the_yaml_file(monkeypatch: pytest.MonkeyPatch, config_file: Path) -> None:
    monkeypatch.setenv(settings_env.CONFIG_FILE_ENV, str(config_file))
    runtime = settings_env.load()
    settings = runtime.settings
    assert settings.remote_url == "https://github.com/chaosbit-dev/datapad.git"
    assert settings.identities == (Identity("o@example.com", "write", "O", "o@example.com"),)
    assert settings.allowed_hosts == ("vault.example.com",)
    assert settings.archive_folder == "archive"
    assert settings.pinned_sources == (
        RefusalSource("chaosbit-dev/holocron:superseded.yaml@main", "yaml-mapping", "datapad/"),
    )
    assert settings.guard_source == RefusalSource("chaosbit-dev/holocron:guards.yaml@main")
    assert settings.freshness_seconds == 15.0
    assert (runtime.host, runtime.port) == ("0.0.0.0", 8000)


def test_environment_overrides_the_file(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    monkeypatch.setenv(settings_env.CONFIG_FILE_ENV, str(config_file))
    monkeypatch.setenv(f"{P}ALLOWED_HOSTS", json.dumps(["a.example.com", "b.example.com"]))
    monkeypatch.setenv(f"{P}BRANCH", "trunk")
    settings = settings_env.load().settings
    assert settings.allowed_hosts == ("a.example.com", "b.example.com")
    assert settings.branch == "trunk"


# @spec VAULT-ID-005
def test_refusal_sources_without_their_own_credential_refuse_to_start(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    text = "\n".join(
        line for line in config_file.read_text().splitlines() if "refusal_token" not in line
    )
    config_file.write_text(text + "\n")
    monkeypatch.setenv(settings_env.CONFIG_FILE_ENV, str(config_file))
    with pytest.raises(StartupError):
        settings_env.load()


def test_a_malformed_refusal_location_is_a_settings_error(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    config_file.write_text(
        config_file.read_text().replace("chaosbit-dev/holocron:guards.yaml@main", "guards.yaml")
    )
    monkeypatch.setenv(settings_env.CONFIG_FILE_ENV, str(config_file))
    with pytest.raises(ValueError):
        settings_env.load()


# @spec VAULT-ACC-008
def test_main_exits_non_zero_on_empty_allowed_hosts(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    monkeypatch.setenv(settings_env.CONFIG_FILE_ENV, str(config_file))
    monkeypatch.setenv(f"{P}ALLOWED_HOSTS", "[]")
    with pytest.raises(SystemExit) as raised:
        server.main()
    assert raised.value.code == 1


# @spec VAULT-ID-001
def test_main_exits_non_zero_on_an_empty_identity_map(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    monkeypatch.setenv(settings_env.CONFIG_FILE_ENV, str(config_file))
    monkeypatch.setenv(f"{P}IDENTITIES", "[]")
    with pytest.raises(SystemExit) as raised:
        server.main()
    assert raised.value.code == 1


def test_main_does_not_echo_a_bad_setting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], config_file: Path
) -> None:
    monkeypatch.setenv(settings_env.CONFIG_FILE_ENV, str(config_file))
    monkeypatch.setenv(f"{P}GITHUB_INSTALLATION_ID", "secret-looking-value")
    with pytest.raises(SystemExit):
        server.main()
    assert "secret-looking-value" not in capsys.readouterr().err


# @spec VAULT-REFUSE-002
def test_a_file_refusal_location_is_accepted(
    monkeypatch: pytest.MonkeyPatch, config_file: Path, tmp_path: Path
) -> None:
    pins = tmp_path / "pins.txt"
    pins.write_text("README.md\n")
    monkeypatch.setenv(settings_env.CONFIG_FILE_ENV, str(config_file))
    monkeypatch.setenv(
        f"{P}PINNED_SOURCES",
        json.dumps(
            [
                {
                    "location": "chaosbit-dev/holocron:superseded.yaml@main",
                    "extraction": "yaml-mapping",
                    "prefix": "datapad/",
                },
                {"location": f"file:{pins}"},
            ]
        ),
    )
    settings = settings_env.load().settings
    assert settings.pinned_sources[1] == RefusalSource(f"file:{pins}")


def test_file_only_refusal_sources_need_no_credential(
    monkeypatch: pytest.MonkeyPatch, key_file: Path, tmp_path: Path
) -> None:
    pins = tmp_path / "pins.txt"
    pins.write_text("README.md\n")
    for name, value in {
        "REMOTE_URL": "https://github.com/chaosbit-dev/datapad.git",
        "ALLOWED_HOSTS": json.dumps(["vault.example.com"]),
        "ACCESS_TEAM_DOMAIN": "team.cloudflareaccess.com",
        "ACCESS_AUDIENCE": "aud",
        "IDENTITIES": json.dumps(
            [
                {
                    "email": "o@example.com",
                    "mode": "write",
                    "author_name": "O",
                    "author_email": "o@example.com",
                }
            ]
        ),
        "PINNED_SOURCES": json.dumps([{"location": f"file:{pins}"}]),
        "GITHUB_APP_ID": "1",
        "GITHUB_INSTALLATION_ID": "2",
        "GITHUB_APP_PRIVATE_KEY_FILE": str(key_file),
    }.items():
        monkeypatch.setenv(f"{P}{name}", value)
    settings = settings_env.load().settings
    assert settings.pinned_sources == (RefusalSource(f"file:{pins}"),)
