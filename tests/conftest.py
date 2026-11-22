"""Shared fixtures. A bare repository in tmp stands in for GitHub; nothing is mocked in git.

The seeded vault (SEED below) is deliberately small and each file earns its place:

  README.md, ideas/README.md, notes/README.md   pinned by the estate's plain list
  ideas/Alpha.md      frontmatter (status/area/priority); every recognised link form to Beta,
                      and every ignored form (fenced code, inline code, HTML comment, escaped)
  ideas/Beta.md       sections: # Beta > ## Design > ### Sub, ## Notes; a duplicate "## Dup"
  notes/Gamma.md      links to Alpha by name and to Beta by folder path; relative md link back
  ideas/Twin.md, notes/Twin.md   same name, same depth -> ambiguous
  Deep.md, ideas/sub/Deep.md     same name, different depth -> shortest path wins
  ideas/Broken.md     frontmatter that does not parse
  notes/Legacy.md     already contains one guard match
  ideas/Pinned.md     pinned via the superseded-style mapping
  .hidden/Secret.md   dot-directory: invisible
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from obsidian_git_mcp.config import Identity, RefusalSource, Settings
from obsidian_git_mcp.service import VaultService

if TYPE_CHECKING:
    from pathlib import Path

TEAM_DOMAIN = "team.cloudflareaccess.test"
AUDIENCE = "aud-123"
HOST = "vault-mcp.test"
OWNER_EMAIL = "owner@example.com"
READER_EMAIL = "reader@example.com"
GUARD_WORD = "Voldemort"  # stands in for a guarded name; tests may print it, it is a fixture

OWNER = Identity(OWNER_EMAIL, "write", "Owner Person", "owner@example.com")
READER = Identity(READER_EMAIL, "read", "Reader Person", "reader@example.com")

SEED: dict[str, str] = {
    "README.md": "# Vault\n",
    "ideas/README.md": "# Ideas\n",
    "notes/README.md": "# Notes\n",
    "ideas/Alpha.md": (
        "---\nstatus: idea\narea: [HA, Gonk/K3s]\npriority: P2\n# a comment kept verbatim\n"
        'related: "[[Gamma]]"\n---\n'
        "# Alpha\n\n"
        "Plain [[Beta]] and [[Beta#Design|the design]] and [[Beta#^blk]] and [[Beta.md]].\n"
        "Embed ![[Beta]] and folder [[ideas/Beta]].\n"
        "Markdown [b](Beta.md) and [b2](<Beta.md>) and [g](../notes/Gamma.md).\n\n"
        "```\n[[Beta]] in a fence\n```\n\n"
        "Inline `[[Beta]]` and <!-- [[Beta]] --> and \\[[Beta]] are not links.\n"
    ),
    "ideas/Beta.md": (
        "# Beta\n\nIntro.\n\n## Design\n\nDesign text.\n\n### Sub\n\nSub text.\n\n"
        "## Notes\n\nNotes text.\n\n## Dup\n\none\n\n## Dup\n\ntwo\n\n"
        "```\n## Design\n```\n"
    ),
    "notes/Gamma.md": (
        "---\nstatus: reference\ntags: [x, y]\nn: 1\n---\n# Gamma\n\n"
        "See [[Alpha]] and [[ideas/Beta]] and [back](../ideas/Alpha.md).\n"
    ),
    "ideas/Twin.md": "# Twin in ideas\n",
    "notes/Twin.md": "# Twin in notes\n",
    "Deep.md": "# Deep at root\n",
    "ideas/sub/Deep.md": "# Deep nested\n",
    "ideas/Broken.md": "---\nstatus: [unclosed\n---\n# Broken\n",
    "notes/Legacy.md": f"# Legacy\n\nMentions {GUARD_WORD} once.\n",
    "ideas/Pinned.md": "# Pinned\n\nSuperseded elsewhere.\n",
    ".hidden/Secret.md": "# Secret\n",
}

PINNED_MAPPING = RefusalSource("superseded.yaml", "yaml-mapping", "datapad/")
PINNED_LIST = RefusalSource("pins.txt", "list")
GUARDS = RefusalSource("guards.yaml", "list")
REFUSAL_TEXT = {
    "superseded.yaml": (
        "datapad/ideas/Pinned.md: homelab-batch/docs/x.md\nother/a.md: other/b.md\n"
    ),
    "pins.txt": "README.md\nideas/README.md\nnotes/README.md\n",
    "guards.yaml": rf"\b{GUARD_WORD}\b" + "\n",
}


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    full_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", **(env or {})}
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=full_env
    ).stdout


@dataclass
class Remote:
    """The bare repository, plus a second clone playing the operator's Mac."""

    bare: Path
    mac: Path
    branch: str = "main"

    def show(self, path: str) -> str | None:
        try:
            return git(self.bare, "show", f"{self.branch}:{path}")
        except subprocess.CalledProcessError:
            return None

    def files_lines(self) -> list[str]:
        """Every path on the branch, unescaped (``-z``: git otherwise quotes non-ASCII)."""
        out = git(self.bare, "ls-tree", "-r", "-z", "--name-only", self.branch)
        return [p for p in out.split("\0") if p]

    def files(self) -> set[str]:
        return set(self.files_lines())

    def head(self) -> str:
        return git(self.bare, "rev-parse", self.branch).strip()

    def commits(self) -> int:
        return int(git(self.bare, "rev-list", "--count", self.branch).strip())

    def last_commit(self) -> dict[str, str]:
        fmt = "%an%x00%ae%x00%cn%x00%B"
        an, ae, cn, body = git(self.bare, "log", "-1", f"--format={fmt}", self.branch).split("\0")
        return {"author_name": an, "author_email": ae, "committer_name": cn, "message": body}

    def mac_commit(self, files: dict[str, str | None], message: str = "mac edit") -> str:
        """Commit as the operator from the Mac clone and push. None deletes a file."""
        git(self.mac, "pull", "--rebase", "-q")
        for path, text in files.items():
            p = self.mac / path
            if text is None:
                git(self.mac, "rm", "-q", path)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
                git(self.mac, "add", path)
        git(self.mac, "commit", "-q", "-m", message)
        git(self.mac, "push", "-q", "origin", self.branch)
        return self.head()

    def force_push_rewrite(self) -> None:
        """Rewrite history on the remote (amend the tip and force-push)."""
        git(self.mac, "pull", "--rebase", "-q")
        git(self.mac, "commit", "-q", "--amend", "-m", "rewritten")
        git(self.mac, "push", "-q", "--force", "origin", self.branch)


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    mac = tmp_path / "mac"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "config", "user.name", "Seeder")
    git(seed, "config", "user.email", "seed@example.com")
    for path, text in SEED.items():
        p = seed / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(bare), str(mac))
    git(mac, "config", "user.name", "Mac Operator")
    git(mac, "config", "user.email", "mac@example.com")
    return Remote(bare, mac)


@dataclass
class FakeTokens:
    calls: list[str] = field(default_factory=list)

    def vault_token(self, permission: str) -> str:
        self.calls.append(permission)
        return f"tok-{permission}-{len(self.calls)}"


@dataclass
class FakeRefusals:
    text: dict[str, str] = field(default_factory=lambda: dict(REFUSAL_TEXT))
    unreadable: set[str] = field(default_factory=set)
    reads: list[str] = field(default_factory=list)

    def read(self, source: RefusalSource) -> str:
        self.reads.append(source.location)
        if source.location in self.unreadable or source.location not in self.text:
            raise OSError(f"cannot read {source.location}")
        return self.text[source.location]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_settings(remote: Remote, workdir: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        remote_url=str(remote.bare),
        branch="main",
        workdir=str(workdir),
        identities=(OWNER, READER),
        allowed_hosts=(HOST,),
        access_team_domain=TEAM_DOMAIN,
        access_audience=AUDIENCE,
        archive_folder="archive",
        pinned_sources=(PINNED_MAPPING, PINNED_LIST),
        guard_source=GUARDS,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def tokens() -> FakeTokens:
    return FakeTokens()


@pytest.fixture
def refusals() -> FakeRefusals:
    return FakeRefusals()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(remote: Remote, tmp_path: Path) -> Settings:
    return make_settings(remote, tmp_path / "work")


@pytest.fixture
def service(
    settings: Settings, tokens: FakeTokens, refusals: FakeRefusals, clock: Clock
) -> VaultService:
    svc = VaultService(settings, tokens, refusals, clock=clock)
    svc.start()
    return svc


# --- Cloudflare Access ----------------------------------------------------------------------


class AccessKeys:
    """The Access team's signing keys, served the way Cloudflare serves them."""

    def __init__(self) -> None:
        self.fetches = 0
        self._keys: dict[str, rsa.RSAPrivateKey] = {}
        self.current = self.rotate()

    def rotate(self) -> str:
        kid = f"kid-{len(self._keys) + 1}"
        self._keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.current = kid
        return kid

    def transport(self) -> httpx2.MockTransport:
        def handle(request: httpx2.Request) -> httpx2.Response:
            assert request.url.host == TEAM_DOMAIN
            assert request.url.path == "/cdn-cgi/access/certs"
            self.fetches += 1
            keys = []
            for kid, key in self._keys.items():
                jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
                keys.append({**jwk, "kid": kid, "alg": "RS256", "use": "sig"})
            return httpx2.Response(200, json={"keys": keys})

        return httpx2.MockTransport(handle)

    def token(
        self,
        email: str = OWNER_EMAIL,
        *,
        kid: str | None = None,
        key: rsa.RSAPrivateKey | None = None,
        **overrides: Any,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "aud": [AUDIENCE],
            "iss": f"https://{TEAM_DOMAIN}",
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "email": email,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        kid = kid or self.current
        signing_key: Any = key or self._keys.get(kid) or next(iter(self._keys.values()))
        return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def access_keys() -> AccessKeys:
    return AccessKeys()
