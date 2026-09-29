"""Stand-ins for `claude`, `git` and `uv`, so `slancha init` never runs the real ones in a test.

Each fake appends its argv to $HOME/tools.log as a JSON line. `claude` keeps its marketplaces and
plugins in $HOME/fake-claude.json and, like the real one, writes enabledPlugins into
settings.json when it installs a plugin. Knobs, all environment variables:

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


def claude(args: list[str]) -> int:
    state = json.loads(STATE.read_text()) if STATE.exists() else {"markets": {}, "plugins": []}
    if args == ["plugin", "list", "--json"]:
        print(json.dumps([{"id": p, "scope": "user", "enabled": True} for p in state["plugins"]]))
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
