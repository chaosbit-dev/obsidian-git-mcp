"""Git subprocess calls against the working clone.

Tokens reach git only through an in-memory credential helper: the helper is a shell function
passed with `-c credential.helper=...` that prints the token from an environment variable of
that one git process. Nothing is written to a file, the remote URL never carries a token, and
git's own error text is scrubbed of the token before it goes anywhere (VAULT-ID-006).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

TOKEN_ENV = "OBSIDIAN_GIT_MCP_TOKEN"

# Settings every git call runs with. The helper list is reset first so no system or user helper
# (osxkeychain, store) ever sees, or saves, a token.
_BASE_CONFIG = (
    "core.precomposeunicode=false",
    "core.quotepath=false",
    "credential.helper=",
    'credential.helper=!f() { test "$1" = get || exit 0; '
    f'printf "username=x-access-token\\npassword=%s\\n" "${TOKEN_ENV}"; }}; f',
    "advice.detachedHead=false",
    "gc.auto=0",
)


class GitError(Exception):
    """A git command failed. `output` is git's stderr with any token removed."""

    def __init__(self, args: Sequence[str], returncode: int, output: str) -> None:
        self.git_args = tuple(args)
        self.returncode = returncode
        self.output = output
        super().__init__(f"git {args[0] if args else ''} failed ({returncode})")


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    kind: str  # blob, tree, commit
    oid: str
    path: str  # repository-relative

    @property
    def is_symlink(self) -> bool:
        return self.mode == "120000"

    @property
    def is_file(self) -> bool:
        return self.kind == "blob" and self.mode in ("100644", "100755")


# @spec VAULT-ID-006
def _env(token: str | None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_") or k in ("GIT_EXEC_PATH", "GIT_SSH_COMMAND")
    }
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "LC_ALL": "C",
        }
    )
    env.pop(TOKEN_ENV, None)
    if token is not None:
        env[TOKEN_ENV] = token
    return env


def run(
    cwd: str,
    args: Sequence[str],
    *,
    token: str | None = None,
    stdin: bytes | None = None,
    extra_env: dict[str, str] | None = None,
) -> bytes:
    """Run git in `cwd` and return stdout. Raises GitError with scrubbed stderr."""
    command = ["git"]
    for item in _BASE_CONFIG:
        command += ["-c", item]
    command += list(args)
    env = _env(token)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(command, cwd=cwd, input=stdin, capture_output=True, env=env, check=False)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        if token:
            err = err.replace(token, "***")
        raise GitError(list(args), proc.returncode, err)
    return proc.stdout


class Repo:
    """The working clone at `path`."""

    def __init__(self, path: str) -> None:
        self.path = path

    def git(
        self,
        *args: str,
        token: str | None = None,
        stdin: bytes | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> bytes:
        return run(self.path, args, token=token, stdin=stdin, extra_env=extra_env)

    def text(self, *args: str) -> str:
        return self.git(*args).decode("utf-8", "surrogateescape").strip()

    # reading ---------------------------------------------------------------------------------
    def head(self) -> str:
        return self.text("rev-parse", "--verify", "HEAD^{commit}")

    def tree(self, rev: str = "HEAD") -> list[TreeEntry]:
        out = self.git("ls-tree", "-r", "-z", "--full-tree", rev)
        entries: list[TreeEntry] = []
        for raw in out.split(b"\0"):
            if not raw:
                continue
            meta, _, path = raw.partition(b"\t")
            mode, kind, oid = meta.decode().split(" ")
            entries.append(TreeEntry(mode, kind, oid, path.decode("utf-8", "surrogateescape")))
        return entries

    def blobs(self, oids: Iterable[str]) -> dict[str, bytes]:
        wanted = list(dict.fromkeys(oids))
        if not wanted:
            return {}
        out = self.git("cat-file", "--batch", stdin=("\n".join(wanted) + "\n").encode())
        found: dict[str, bytes] = {}
        pos = 0
        for oid in wanted:
            nl = out.index(b"\n", pos)
            header = out[pos:nl].decode().split(" ")
            if len(header) < 3 or header[1] == "missing":
                raise GitError(["cat-file"], 1, f"object {oid} missing")
            size = int(header[2])
            start = nl + 1
            found[oid] = out[start : start + size]
            pos = start + size + 1
        return found

    def last_change_times(self) -> dict[str, int]:
        """Committer time of the newest commit that touched each path (first seen wins)."""
        out = self.git("log", "--format=%x01%ct", "--name-only", "-z", "--no-renames", "HEAD")
        times: dict[str, int] = {}
        for chunk in out.split(b"\x01"):
            if not chunk:
                continue
            stamp, _, rest = chunk.partition(b"\0")
            when = int(stamp.decode().strip())
            for raw in rest.split(b"\0"):
                path = raw.strip(b"\n").decode("utf-8", "surrogateescape")
                if path and path not in times:
                    times[path] = when
        return times

    # writing ---------------------------------------------------------------------------------
    def hash_blob(self, data: bytes) -> str:
        return self.git("hash-object", "-w", "--stdin", stdin=data).decode().strip()

    def blob_id(self, data: bytes) -> str:
        """The id `data` would have as a blob, without storing it."""
        return self.git("hash-object", "--stdin", stdin=data).decode().strip()

    def write_tree(self, base: str, changes: dict[str, tuple[str, str] | None]) -> str:
        """A tree: `base`'s tree with each path set to (mode, oid), or removed for None."""
        index = os.path.join(self.path, ".git", "obsidian-git-mcp.index")
        env = {"GIT_INDEX_FILE": index}
        try:
            self.git("read-tree", base, extra_env=env)
            lines = b""
            for path, entry in changes.items():
                raw = path.encode("utf-8", "surrogateescape")
                if entry is None:
                    lines += b"0 " + b"0" * 40 + b"\t" + raw + b"\0"
                else:
                    mode, oid = entry
                    lines += f"{mode} {oid}".encode() + b"\t" + raw + b"\0"
            if lines:
                self.git("update-index", "-z", "--index-info", stdin=lines, extra_env=env)
            return self.git("write-tree", extra_env=env).decode().strip()
        finally:
            if os.path.exists(index):
                os.unlink(index)

    def commit_tree(
        self,
        tree: str,
        parent: str,
        message: str,
        *,
        author: tuple[str, str],
        committer: tuple[str, str],
    ) -> str:
        env = {
            "GIT_AUTHOR_NAME": author[0],
            "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": committer[0],
            "GIT_COMMITTER_EMAIL": committer[1],
        }
        out = self.git(
            "commit-tree", tree, "-p", parent, "-F", "-", stdin=message.encode(), extra_env=env
        )
        return out.decode().strip()
