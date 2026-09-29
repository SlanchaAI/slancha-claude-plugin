"""The plugin inside the real Claude Code binary, against a scripted fake model (fake_model.py)
and the fake Slancha API (../fake_api.py). No account, key or network is needed.

Run by scripts/e2e.sh, inside tests/e2e/Dockerfile, with the repository mounted at /plugin.
Each `claude -p` run is one session. capture-settings.json adds hooks that save the raw JSON
each event delivers, so the payload shapes the plugin relies on are checked as Claude Code sends
them.
"""
import glob
import io
import json
import os
import subprocess
import sys
import time
import zipfile

sys.path[:0] = ["/plugin/tests", "/plugin/tests/e2e"]
from fake_api import TOKEN, FakeSlancha, skill_zip  # noqa: E402
from fake_model import FakeModel  # noqa: E402

HOME, PROJECT, CAPTURE = "/tmp/home", "/tmp/project", "/tmp/capture"
SPOOL = f"{HOME}/.cache/slancha/usage.jsonl"
DESCRIPTION = "Demo skill for the end-to-end run."
V1 = {"SKILL.md": f"---\nname: demo\ndescription: {DESCRIPTION}\n---\nSay hi.\n"}
V2 = {"SKILL.md": f"---\nname: demo\ndescription: {DESCRIPTION}\n---\nSay hi twice.\n"}

slancha, model = FakeSlancha(), FakeModel()
for directory in (HOME, PROJECT, CAPTURE):
    os.makedirs(directory, exist_ok=True)
ENV = {"HOME": HOME, "PATH": os.environ["PATH"], "ANTHROPIC_BASE_URL": model.url,
       "ANTHROPIC_API_KEY": "test-key-not-real", "SLANCHA_API_URL": slancha.url,
       "SLANCHA_TOKEN": TOKEN, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
       "DISABLE_AUTOUPDATER": "1"}
failures = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n      {str(detail)[:600]}"))
    if not ok:
        failures.append(name)


def claude(prompt, plan=()):
    """One -p session; returns its stream-json events and the model requests it made."""
    model.plan = list(plan)
    first = len(model.requests)
    run = subprocess.run(["claude", "-p", prompt, "--plugin-dir", "/plugin",
                          "--settings", "/plugin/tests/e2e/capture-settings.json",
                          "--output-format", "stream-json", "--verbose"],
                         env=ENV, cwd=PROJECT, capture_output=True, text=True, timeout=180)
    events = []
    for line in run.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events, model.requests[first:]


def recorded():
    """Usage events so far: still spooled, or already uploaded by the Stop hook's flush."""
    try:
        with open(SPOOL) as handle:
            waiting = [json.loads(line) for line in handle.read().splitlines()]
    except OSError:
        waiting = []
    return waiting + list(slancha.usage.values())


def captured(event):
    out = []
    for path in sorted(glob.glob(f"{CAPTURE}/{event}-*.json")):
        with open(path) as handle:
            try:
                out.append(json.load(handle))
            except ValueError:
                pass
    return out


def sent_to_model(requests):
    return json.dumps([request.get("messages") for request in requests])


# /slancha:update <skill> installs through the command's injected shell line.
v1 = slancha.publish("demo", V1)
events, requests = claude("/slancha:update demo")
check("/slancha:update ran the script and Claude received its report",
      f"demo: installed {v1[:12]}" in sent_to_model(requests), sent_to_model(requests)[-600:])
check("the skill is in ~/.claude/skills/demo",
      os.path.isfile(f"{HOME}/.claude/skills/demo/slancha-manifest.json"))
check("the plugin's own command is not recorded as usage", recorded() == [], recorded())

# The user types /demo: UserPromptExpansion names it; recorded with success null.
events, requests = claude("/demo please")
typed = [e for e in captured("UserPromptExpansion") if e.get("command_name") == "demo"]
check("UserPromptExpansion delivers expansion_type, command_name and prompt_id",
      typed and typed[-1].get("expansion_type") == "slash_command" and typed[-1].get("prompt_id"),
      typed)
check("typed /demo is recorded (success null, installed revision)",
      [e for e in recorded() if e["success"] is None and e["revision"] == v1], recorded())

# Claude calls the Skill tool: PostToolUse with matcher Skill; recorded with success true.
events, requests = claude("use the demo skill",
                          plan=[("tool", "Skill", {"skill": "demo"}), ("text", "Done.")])
posts = captured("PostToolUse")
check("PostToolUse(Skill) delivers tool_input.skill and tool_use_id",
      posts and posts[-1].get("tool_input", {}).get("skill") == "demo"
      and posts[-1].get("tool_use_id"), posts)
check("the Skill call is recorded (success true)",
      [e for e in recorded() if e["success"] is True], recorded())

# An unknown skill is refused before it runs, so no PostToolUseFailure fires (only noted).
events, requests = claude("use a missing skill",
                          plan=[("tool", "Skill", {"skill": "no-such"}), ("text", "Done.")])
print(f"NOTE  PostToolUseFailure events for an unknown skill: {len(captured('PostToolUseFailure'))}")

# The synchronous Stop hook starts the detached upload; -p sessions skip async hooks and
# rarely reach SessionEnd, so this is the path the usage takes from a -p run.
deadline = time.time() + 20
while len(slancha.usage) < 2 and time.time() < deadline:
    time.sleep(0.5)
check("usage reached the Slancha API",
      {(e["skill"], e["success"]) for e in slancha.usage.values()} >= {("demo", None),
                                                                      ("demo", True)},
      list(slancha.usage.values()))
check("uploaded events hold no content",
      all(set(e) == {"id", "skill", "revision", "kind", "success", "session", "at"}
          for e in slancha.usage.values()))

# Once a day, SessionStart says when a newer revision is published.
v2 = slancha.publish("demo", V2)
stamp = f"{HOME}/.cache/slancha/update-check.stamp"
if os.path.exists(stamp):
    os.unlink(stamp)
events, requests = claude("hello")
starts = [e.get("stdout", "") for e in events
          if e.get("subtype") == "hook_response" and e.get("hook_event") == "SessionStart"]
check("SessionStart answers with a systemMessage naming the update",
      any("updates available for demo" in out and "systemMessage" in out for out in starts),
      starts)

# A managed skill committed in the project's .claude/skills is updated where it lives.
p1 = slancha.publish("proj", {"SKILL.md": "Project skill.\n"})
with zipfile.ZipFile(io.BytesIO(skill_zip("proj", slancha.published["proj"][1], p1))) as bundle:
    bundle.extractall(f"{PROJECT}/.claude/skills")
p2 = slancha.publish("proj", {"SKILL.md": "Project skill, second revision.\n"})

# /slancha:update moves both to their new revisions and keeps one backup of each.
events, requests = claude("/slancha:update")
report = sent_to_model(requests)
check("demo updated to the new revision", f"demo: updated {v1[:12]} -> {v2[:12]}" in report,
      report[-600:])
check("the project skill is updated in place", f"proj: updated {p1[:12]} -> {p2[:12]}" in report
      and "second revision" in open(f"{PROJECT}/.claude/skills/proj/SKILL.md").read(), report[-600:])
check("the previous copy is kept in .slancha-backup",
      os.path.isfile(f"{HOME}/.claude/skills/.slancha-backup/demo/SKILL.md"))

# Claude Code does not load the backup as a second skill.
events, requests = claude("hello")
init = next((e for e in events if e.get("subtype") == "init"), {})
check("the backup is not offered as a skill",
      init.get("skills", []).count("demo") == 1 and json.dumps(requests).count(DESCRIPTION) <= 1
      and ".slancha-backup" not in json.dumps(requests), init.get("skills"))

events, requests = claude("/slancha:status")
check("/slancha:status reports", "Slancha plugin" in sent_to_model(requests)
      and "demo" in sent_to_model(requests), sent_to_model(requests)[-600:])

print(f"\n{'all checks passed' if not failures else f'{len(failures)} failed: {failures}'}")
sys.exit(1 if failures else 0)
