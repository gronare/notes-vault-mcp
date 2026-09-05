FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

LABEL org.opencontainers.image.source="https://github.com/gronare/notes-vault-mcp"
LABEL io.modelcontextprotocol.server.name="io.github.gronare/notes-vault-mcp"

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md ./
COPY notes_vault_mcp ./notes_vault_mcp
RUN uv sync --frozen --no-dev

ENTRYPOINT ["notes-vault-mcp"]
