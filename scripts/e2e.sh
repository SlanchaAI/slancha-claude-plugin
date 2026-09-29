#!/usr/bin/env bash
# Validate the plugin with Claude Code, then run it end to end in the real Claude Code binary
# against a fake model and a fake Slancha API (tests/e2e/run.py). Runs in Docker; building the
# image needs the npm registry, the run itself needs no account, key or network.
# CLAUDE_CODE_VERSION=2.1.284 scripts/e2e.sh pins the Claude Code version (default: latest).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
image=slancha-plugin-e2e
docker build -q -t "$image" --build-arg "CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION:-latest}" \
  "$root/tests/e2e" >/dev/null
docker run --rm --memory 3g -v "$root:/plugin:ro" "$image" sh -ec '
  claude --version
  cd /plugin
  claude plugin validate --strict .           # the marketplace manifest
  claude plugin validate --strict commands    # the command files
  # A directory holding marketplace.json validates as a marketplace; without it, as a plugin,
  # which is what checks plugin.json and hooks/hooks.json.
  cp -r /plugin /tmp/plugin && rm /tmp/plugin/.claude-plugin/marketplace.json
  claude plugin validate --strict /tmp/plugin
  python3 /plugin/tests/e2e/run.py
'
