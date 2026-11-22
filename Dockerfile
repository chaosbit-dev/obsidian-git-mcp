# @spec VAULT-DEPLOY-003
# Base images are pinned by digest: a tag can be moved to different content, a digest cannot.
# The image holds a credential that can write to the vault, so what it runs must be exact.
# python:3.13-slim and ghcr.io/astral-sh/uv:0.13.7, resolved 2026-11-21.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/

RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The server drives the real git binary against its clone.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# A fixed non-root uid with a home, so git has somewhere for its (empty) global config.
RUN groupadd --gid 65532 vault \
    && useradd --uid 65532 --gid 65532 --create-home --home-dir /home/vault vault \
    && install -d -o 65532 -g 65532 /vault

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

# PYTHONUNBUFFERED: the audit log is stdout; a buffered line is a lost line when the pod is killed.
# GIT_TERMINAL_PROMPT=0: a missing token must fail the fetch, never hang it on a prompt.
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/home/vault \
    OBSIDIAN_GIT_MCP_WORKDIR=/vault \
    PYTHONUNBUFFERED=1 \
    GIT_TERMINAL_PROMPT=0

USER 65532:65532

EXPOSE 8000

# The console script, never `python -m obsidian_git_mcp.server`: that loads the module twice.
CMD ["obsidian-git-mcp"]
