FROM python:3.12-slim

WORKDIR /app

# Install Node.js + Anthropic Claude Code CLI so the cockpit /chat endpoint
# can call the LLM as a subprocess (Anthropic blocked direct OAuth
# /v1/messages calls in Jan 2026; the CLI is the supported path for
# Claude Max billing).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_server ./mcp_server
COPY servers ./servers
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e .

ENV PORT=8080
EXPOSE 8080

CMD ["motto-mcp-server"]
