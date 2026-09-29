#!/bin/sh
# Set up Slancha for Claude Code on this machine:
#
#   curl -fsSL https://dev.slancha.ai/install.sh | sh
#
# Downloads scripts/slancha.py from SlanchaAI/slancha-claude-plugin at a pinned commit, checks its
# sha256, installs it as ~/.local/bin/slancha, then runs `slancha auth` (an org admin approves the
# sign-in) and `slancha init` (the Claude Code plugins and tracing settings). SLANCHA_API_URL
# picks another Slancha API.
#
# The release step replaces both placeholders: the commit (or tag) to install from, and the
# sha256 of scripts/slancha.py at that commit.
set -eu

SLANCHA_REF="${SLANCHA_REF:-REF_PLACEHOLDER}"
SLANCHA_SHA256="${SLANCHA_SHA256:-SHA256_PLACEHOLDER}"

# Everything runs from main, called on the last line, so a download cut short runs nothing.
main() {
  say() { printf '%s\n' "$*"; }
  fail() { say "slancha install: $*" >&2; exit 1; }

  case "$SLANCHA_REF$SLANCHA_SHA256" in
    *PLACEHOLDER*) fail "this install.sh is not a release: its commit and sha256 are not filled in." ;;
  esac
  [ "$(id -u)" != 0 ] || fail "run it as yourself, not as root: it sets up your own ~/.claude."
  command -v curl >/dev/null 2>&1 || fail "curl is not on PATH."
  command -v python3 >/dev/null 2>&1 || fail "python3 (3.9 or newer) is not on PATH."
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
    || fail "python3 is older than 3.9; install a newer one."
  if command -v sha256sum >/dev/null 2>&1; then
    sha() { sha256sum "$1" | cut -d' ' -f1; }
  elif command -v shasum >/dev/null 2>&1; then
    sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
  else
    fail "neither sha256sum nor shasum is on PATH."
  fi

  url="https://raw.githubusercontent.com/SlanchaAI/slancha-claude-plugin/$SLANCHA_REF/scripts/slancha.py"
  share="$HOME/.local/share/slancha"
  bin="$HOME/.local/bin"
  say "This will:"
  say "  1. download $url"
  say "     and check its sha256 ($SLANCHA_SHA256)"
  say "  2. install it as $share/slancha.py, with the command $bin/slancha"
  say "  3. run 'slancha auth': sign in to Slancha${SLANCHA_API_URL:+ at $SLANCHA_API_URL}; an admin of your org approves it"
  say "  4. run 'slancha init': install the Langfuse and Slancha plugins for Claude Code and write"
  say "     the tracing keys to ~/.claude/settings.json (mode 0600)"
  say ""

  umask 077
  mkdir -p "$share" "$bin"
  tmp="$(mktemp "$share/slancha.py.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  curl -fsSL --proto '=https' --tlsv1.2 -o "$tmp" "$url" || fail "could not download $url"
  got="$(sha "$tmp")"
  [ "$got" = "$SLANCHA_SHA256" ] || fail "the download's sha256 is $got, not $SLANCHA_SHA256; nothing installed."
  chmod 644 "$tmp"
  mv "$tmp" "$share/slancha.py"
  printf '#!/bin/sh\nexec python3 "%s" "$@"\n' "$share/slancha.py" > "$bin/slancha"
  chmod 755 "$bin/slancha"
  say "Installed $bin/slancha."
  case ":$PATH:" in
    *":$bin:"*) ;;
    *) say "Note: $bin is not on your PATH; add it to run slancha by name." ;;
  esac
  say ""

  # stdin is this script (curl | sh); the commands read the terminal instead.
  tty=/dev/null
  if (: </dev/tty) 2>/dev/null; then tty=/dev/tty; fi
  "$bin/slancha" auth <"$tty"
  say ""
  "$bin/slancha" init <"$tty"
}

main "$@"
