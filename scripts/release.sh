#!/usr/bin/env bash
# Cut a release of the plugin and its installer.
#
#   scripts/release.sh version 0.2.0   set the version in .claude-plugin/plugin.json and in
#                                      scripts/slancha.py (VERSION); commit and push it yourself
#   scripts/release.sh installer       print install.sh pinned to HEAD: its commit, and the sha256
#                                      of scripts/slancha.py at that commit, for slancha-app's
#                                      site/public/install.sh
#
# Claude Code updates an installed plugin only when its version changes, so every release bumps
# it. The released installer pins the commit and digest outright: it takes neither from the
# environment, so nobody can point a copied one-liner at other code.
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-}" in
  version)
    version="${2:-}"
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: $0 version X.Y.Z" >&2; exit 2; }
    sed -i -E "s/^VERSION = \"[^\"]*\"/VERSION = \"$version\"/" scripts/slancha.py
    sed -i -E "s/(\"version\": )\"[^\"]*\"/\1\"$version\"/" .claude-plugin/plugin.json
    if ! grep -q "^VERSION = \"$version\"$" scripts/slancha.py \
        || ! grep -q "\"version\": \"$version\"" .claude-plugin/plugin.json; then
      echo "version not set in both files" >&2
      exit 1
    fi
    echo "Version $version in scripts/slancha.py and .claude-plugin/plugin.json."
    echo "Commit and push it, then run: $0 installer"
    ;;
  installer)
    git diff --quiet HEAD -- scripts/slancha.py install.sh \
      || { echo "commit scripts/slancha.py and install.sh first: the pin names a commit" >&2; exit 1; }
    ref="$(git rev-parse HEAD)"
    sha="$(git show "$ref:scripts/slancha.py" | sha256sum | cut -d' ' -f1)"
    sed -e "s/^SLANCHA_REF=.*/SLANCHA_REF=\"$ref\"/" \
        -e "s/^SLANCHA_SHA256=.*/SLANCHA_SHA256=\"$sha\"/" install.sh
    echo "pinned to $ref (slancha.py sha256 $sha); the commit must be on GitHub before anyone runs it" >&2
    ;;
  *)
    echo "usage: $0 version X.Y.Z | $0 installer" >&2
    exit 2
    ;;
esac
