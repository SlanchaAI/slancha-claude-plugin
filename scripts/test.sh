#!/usr/bin/env bash
# The test suite, in Docker, under the oldest and the newest Python the plugin supports.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
for image in python:3.9-slim python:3.12-slim; do
  echo "== $image"
  docker run --rm --memory 4g -v "$root:/w:ro" -w /w -e PYTHONDONTWRITEBYTECODE=1 "$image" \
    python -m unittest discover -s tests "$@"
done
