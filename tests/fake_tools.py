"""Stand-ins for `claude`, `git` and `uv`, so `slancha init` never runs the real ones in a test.

Each fake appends its argv to $HOME/tools.log as a JSON line. `claude` keeps its marketplaces and
plugins in $HOME/fake-claude.json and, like the real one, writes enabledPlugins into
settings.json when it installs a plugin, and `plugin enable|disable --scope user|local --json`
into settings.json or the current directory's .claude/settings.local.json. Knobs, all
environment variables:

- FAKE_GIT_BAD_CHECKOUT=1   git checkout leaves HEAD on another commit
- FAKE_UV_NO_PYTHON=1       `uv python find` finds nothing
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOME = Path(os.environ["HOME"])
STATE = HOME / "fake-claude.json"


def settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else HOME / ".claude") / "settings.json"


def enabled_in(path: Path) -> dict:
    return json.loads(path.read_text()).get("enabledPlugins", {}) if path.exists() else {}


def claude(args: list[str]) -> int:
    state = json.loads(STATE.read_text()) if STATE.exists() else {"markets": {}, "plugins": []}
    local = Path.cwd() / ".claude" / "settings.local.json"
    if args == ["plugin", "list", "--json"]:
        # the project's local setting wins over the user's, as in Claude Code
        user = enabled_in(settings_path())
        print(json.dumps([{"id": p, "scope": "user",
                           "enabled": enabled_in(local).get(p, user.get(p, True))}
                          for p in state["plugins"]]))
    elif args[:2] in (["plugin", "enable"], ["plugin", "disable"]) \
            and args[3:5] in (["--scope", "user"], ["--scope", "local"]) and args[5:] == ["--json"]:
        # --json prints one result line; "already" in that state is exit 1, as in Claude Code
        on, path = args[1] == "enable", settings_path() if args[4] == "user" else local
        user = enabled_in(settings_path()).get(args[2], True)
        if args[2] not in state["plugins"] or enabled_in(path).get(args[2], user) == on:
            print(json.dumps({"command": args[1], "outcome": "failed", "plugin": args[2],
                              "alreadyInGoalState": args[2] in state["plugins"]}))
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        settings = json.loads(path.read_text()) if path.exists() else {}
        settings.setdefault("enabledPlugins", {})[args[2]] = on
        path.write_text(json.dumps(settings, indent=2))
        print(json.dumps({"command": args[1], "outcome": "succeeded", "plugin": args[2]}))
    elif args == ["plugin", "marketplace", "list", "--json"]:
        print(json.dumps([{"name": n, "source": "directory", "path": p}
                          for n, p in state["markets"].items()]))
    elif args[:3] == ["plugin", "marketplace", "add"]:
        name = "langfuse-observability" if "langfuse" in args[3] else "slancha"
        state["markets"][name] = args[3]
    elif args[:2] == ["plugin", "install"] and args[3:] == ["--scope", "user"]:
        if args[2].split("@")[1] not in state["markets"]:
            print(f"marketplace of {args[2]} not found", file=sys.stderr)
            return 1
        state["plugins"].append(args[2])
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        settings = json.loads(path.read_text()) if path.exists() else {}
        settings.setdefault("enabledPlugins", {})[args[2]] = True
        path.write_text(json.dumps(settings, indent=2))
    elif args[:2] == ["plugin", "uninstall"] and args[3:] == ["--scope", "user"]:
        if args[2] not in state["plugins"]:
            print(f"{args[2]} is not installed", file=sys.stderr)
            return 1
        state["plugins"].remove(args[2])
        path = settings_path()
        settings = json.loads(path.read_text()) if path.exists() else {}
        settings.get("enabledPlugins", {}).pop(args[2], None)
        path.write_text(json.dumps(settings, indent=2))
    elif args[:3] == ["plugin", "marketplace", "remove"]:
        if state["markets"].pop(args[3], None) is None:
            print(f"marketplace {args[3]} not found", file=sys.stderr)
            return 1
    else:
        print(f"fake claude: unexpected {args}", file=sys.stderr)
        return 2
    STATE.write_text(json.dumps(state))
    return 0


def git(args: list[str]) -> int:
    if args[:2] == ["clone", "--quiet"]:
        (Path(args[3]) / ".git").mkdir(parents=True)
        (Path(args[3]) / ".git" / "FAKE_HEAD").write_text("0" * 40)
        return 0
    if args[0] != "-C":
        return 2
    head = Path(args[1]) / ".git" / "FAKE_HEAD"
    if args[2:] == ["rev-parse", "HEAD"]:
        print(head.read_text())
    elif args[2:4] == ["cat-file", "-e"]:
        return 0
    elif args[2:5] == ["checkout", "--quiet", "--detach"]:
        head.write_text("f" * 40 if os.environ.get("FAKE_GIT_BAD_CHECKOUT") else args[5])
    else:
        return 2
    return 0


def uv(args: list[str]) -> int:
    if args[:2] == ["python", "find"]:
        if os.environ.get("FAKE_UV_NO_PYTHON"):
            return 2
        print("/usr/bin/python3.12")
        return 0
    return 2


def main(tool: str) -> None:
    args = sys.argv[1:]
    with open(HOME / "tools.log", "a") as log:
        log.write(json.dumps([tool] + args) + "\n")
    sys.exit({"claude": claude, "git": git, "uv": uv}[tool](args))
