"""Loading the server's configuration from the environment and an optional YAML file.

Every setting is read from an environment variable named `OBSIDIAN_GIT_MCP_<FIELD>` (list and
mapping values as JSON), or from a YAML file whose path is in `OBSIDIAN_GIT_MCP_CONFIG_FILE`.
The environment wins over the file. A YAML file suits the structured settings:

    remote_url: https://github.com/chaosbit-dev/datapad.git
    branch: main
    workdir: /vault
    allowed_hosts: [vault-mcp.example.com]
    access_team_domain: team.cloudflareaccess.com
    access_audience: "<Access application AUD tag>"
    identities:
      - {email: owner@example.com, mode: write,
         author_name: Owner Person, author_email: owner@example.com}
    trash_folder: trash
    archive_folder: archive
    pinned_sources:
      - {location: "chaosbit-dev/holocron:superseded.yaml@main",
         extraction: yaml-mapping, prefix: datapad/}
    guard_source: {location: "chaosbit-dev/holocron:guards.yaml@main"}
    github_app_id: "123456"
    github_installation_id: 7890
    github_app_private_key_file: /secrets/vault-app/private-key.pem
    refusal_token_file: /secrets/refusal-token/token

Credentials are named by file (a mounted Secret) or given as an environment variable; they are
held in memory only and never echoed (VAULT-ID-006). Refusal sources take their own credential:
a fixed read-only token, or a second App (`refusal_app_*`). There is deliberately no fallback to
the vault's App (VAULT-ID-005). Refusal-source locations use the `owner/repo:path[@ref]` form
documented in `refusal_sources`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from ruamel.yaml import YAML

from obsidian_git_mcp.config import Identity, RefusalSource, Settings
from obsidian_git_mcp.errors import StartupError
from obsidian_git_mcp.github_app import DEFAULT_API_URL, GitHubApp, VaultTokens, repository_name
from obsidian_git_mcp.refusal_sources import (
    GitHubRefusalReader,
    parse_location,
    static_credential,
)

if TYPE_CHECKING:
    from collections.abc import Callable

ENV_PREFIX = "OBSIDIAN_GIT_MCP_"
CONFIG_FILE_ENV = f"{ENV_PREFIX}CONFIG_FILE"


class IdentityModel(BaseModel):
    email: str
    # Left as a plain string so `config.validate` reports a bad mode as a StartupError.
    mode: str
    author_name: str = ""
    author_email: str = ""


class RefusalSourceModel(BaseModel):
    location: str
    extraction: Literal["list", "yaml-mapping"] = "list"
    prefix: str = ""

    @model_validator(mode="after")
    def _location_parses(self) -> RefusalSourceModel:
        parse_location(self.location)
        return self


class _YamlFileSource(PydanticBaseSettingsSource):
    """Settings from the YAML file named in OBSIDIAN_GIT_MCP_CONFIG_FILE, if any."""

    def __init__(self, settings_cls: type[BaseSettings], path: str | None) -> None:
        super().__init__(settings_cls)
        self._data: dict[str, Any] = {}
        if path:
            loaded = YAML(typ="safe").load(Path(path).read_text(encoding="utf-8"))
            if loaded is not None and not isinstance(loaded, dict):
                raise StartupError(f"{path} is not a YAML mapping")
            self._data = dict(loaded or {})

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        fields = self.settings_cls.model_fields
        return {k: v for k, v in self._data.items() if k in fields}


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, extra="ignore")

    remote_url: str
    branch: str = "main"
    workdir: str = "/vault"
    identities: list[IdentityModel] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)
    access_team_domain: str
    access_audience: str
    subdirectory: str = ""
    freshness_seconds: float = 15.0
    unhealthy_after_seconds: float = 600.0
    trash_folder: str = "trash"
    archive_folder: str | None = None
    size_cap_bytes: int = 1_000_000
    pinned_sources: list[RefusalSourceModel] = Field(default_factory=list)
    guard_source: RefusalSourceModel | None = None

    host: str = "0.0.0.0"  # a container listens on every interface
    port: int = 8000

    github_api_url: str = DEFAULT_API_URL
    github_app_id: str
    github_installation_id: int
    github_app_private_key: SecretStr | None = None
    github_app_private_key_file: Path | None = None
    # The vault repository's name for token narrowing; derived from remote_url when unset.
    vault_repository: str | None = None

    refusal_token: SecretStr | None = None
    refusal_token_file: Path | None = None
    refusal_app_id: str | None = None
    refusal_installation_id: int | None = None
    refusal_app_private_key_file: Path | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        path = os.environ.get(CONFIG_FILE_ENV)
        return (init_settings, env_settings, _YamlFileSource(settings_cls, path))

    def to_settings(self) -> Settings:
        def source(model: RefusalSourceModel) -> RefusalSource:
            return RefusalSource(model.location, model.extraction, model.prefix)

        return Settings(
            remote_url=self.remote_url,
            branch=self.branch,
            workdir=self.workdir,
            identities=tuple(
                Identity(i.email, i.mode, i.author_name, i.author_email)  # type: ignore[arg-type]
                for i in self.identities
            ),
            allowed_hosts=tuple(h for h in self.allowed_hosts if h.strip()),
            access_team_domain=self.access_team_domain,
            access_audience=self.access_audience,
            subdirectory=self.subdirectory,
            freshness_seconds=self.freshness_seconds,
            unhealthy_after_seconds=self.unhealthy_after_seconds,
            trash_folder=self.trash_folder,
            archive_folder=self.archive_folder or None,
            size_cap_bytes=self.size_cap_bytes,
            pinned_sources=tuple(source(s) for s in self.pinned_sources),
            guard_source=source(self.guard_source) if self.guard_source else None,
        )


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    tokens: VaultTokens
    refusals: GitHubRefusalReader
    host: str
    port: int


def _secret(value: SecretStr | None, path: Path | None, what: str) -> str | None:
    if value is not None and value.get_secret_value().strip():
        return value.get_secret_value().strip()
    if path is not None:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise StartupError(f"cannot read {what} from {path}: {error.strerror}") from None
        if not text:
            raise StartupError(f"{what} file {path} is empty")
        return text
    return None


def _refusal_credential(env: EnvSettings) -> Callable[[str], str]:
    token = _secret(env.refusal_token, env.refusal_token_file, "the refusal-source token")
    if token is not None:
        return static_credential(token)
    if env.refusal_app_id and env.refusal_installation_id is not None:
        key = _secret(None, env.refusal_app_private_key_file, "the refusal App's private key")
        if key is None:
            raise StartupError("refusal_app_id is set without refusal_app_private_key_file")
        app = GitHubApp(
            app_id=env.refusal_app_id,
            installation_id=env.refusal_installation_id,
            private_key=key,
            api_url=env.github_api_url,
        )

        def minted(repository: str) -> str:
            return app.installation_token(repository.rpartition("/")[2], "read")

        return minted

    if env.pinned_sources or env.guard_source:
        # A misconfiguration, not an outage: an unreadable source at runtime still lets the
        # server start (writes refused, reads served), but a source with no credential at all
        # could never be read.
        raise StartupError("refusal sources are configured without a refusal-source credential")

    def unused(_repository: str) -> str:
        raise OSError("no refusal sources are configured")

    return unused


def load() -> Runtime:
    """Build the runtime from the environment and config file.

    Raises StartupError, or pydantic's ValidationError for a malformed setting.
    """
    env = EnvSettings()
    key = _secret(
        env.github_app_private_key, env.github_app_private_key_file, "the App private key"
    )
    if key is None:
        raise StartupError("no GitHub App private key is configured")
    try:
        repository = env.vault_repository or repository_name(env.remote_url)
    except ValueError as error:
        raise StartupError(str(error)) from None
    app = GitHubApp(
        app_id=env.github_app_id,
        installation_id=env.github_installation_id,
        private_key=key,
        api_url=env.github_api_url,
    )
    return Runtime(
        settings=env.to_settings(),
        tokens=VaultTokens(app, repository),
        refusals=GitHubRefusalReader(_refusal_credential(env), api_url=env.github_api_url),
        host=env.host,
        port=env.port,
    )
