# Claude Code and python3, to validate the plugin and run tests/e2e/run.py against it.
FROM node:22-slim
ARG CLAUDE_CODE_VERSION=latest
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && claude --version
