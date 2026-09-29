"""The plugin script against a fake Slancha API. Every test runs scripts/slancha.py as Claude Code
does, in a subprocess, with HOME pointed at a temp directory.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_api import TOKEN, FakeSlancha, entry, sha256, skill_zip  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "slancha.py"

V1 = {"SKILL.md": "---\nname: demo\ndescription: A demo skill.\n---\nDo the thing.\n",
      "references/notes.md": "Notes, first cut.\n",
      "references/old.md": "Dropped in the second revision.\n"}
V2 = {"SKILL.md": "---\nname: demo\ndescription: A demo skill.\n---\nDo the thing better.\n",
      "references/notes.md": "Notes, second cut.\n",
      "scripts/run.py": "print('hello')\n"}


def closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


class PluginCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.home.mkdir()
        self.project.mkdir()
        self.api = FakeSlancha()
        self.addCleanup(self.api.close)
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "SLANCHA_API_URL": self.api.url, "SLANCHA_TOKEN": TOKEN,
                    "CLAUDE_PROJECT_DIR": str(self.project)}

    def run_script(self, *args, stdin: str = "", env: dict | None = None) -> str:
        result = subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, text=True,
                                capture_output=True, env=env or self.env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    @property
    def skills(self) -> Path:
        return self.home / ".claude" / "skills"

    def tree(self, path: Path) -> dict[str, str]:
        return {p.relative_to(path).as_posix(): p.read_text() for p in sorted(path.rglob("*"))
                if p.is_file() and p.name != "slancha-manifest.json"}

    def revision_of(self, path: Path) -> str:
        return json.loads((path / "slancha-manifest.json").read_text())["revision"]

    def install_v1(self) -> str:
        v1 = self.api.publish("demo", V1)
        self.assertIn("installed", self.run_script("update", "demo"))
        return v1

    def spool(self) -> list[dict]:
        path = self.home / ".cache" / "slancha" / "usage.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


class UpdateTests(PluginCase):
    def test_install_named_skill(self):
        v1 = self.api.publish("demo", V1)
        out = self.run_script("update", "demo")
        self.assertIn(f"demo: installed {v1[:12]} in ~/.claude/skills.", out)
        # ~/.claude/skills did not exist, so Claude Code was not watching it
        self.assertIn("/reload-skills", out)
        self.assertEqual(self.tree(self.skills / "demo"), V1)
        self.assertEqual(self.revision_of(self.skills / "demo"), v1)

        self.api.publish("other", {"SKILL.md": "---\nname: other\n---\nOther.\n"})
        out = self.run_script("update", "other")
        self.assertIn("other: installed", out)
        self.assertNotIn("/reload-skills", out)   # a new skill in a watched directory loads by itself

    def test_update_installs_new_revision(self):
        v1 = self.install_v1()
        v2 = self.api.publish("demo", V2)
        out = self.run_script("update")
        self.assertIn(f"demo: updated {v1[:12]} -> {v2[:12]}", out)
        self.assertEqual(self.tree(self.skills / "demo"), V2)      # old.md is gone
        self.assertEqual(self.revision_of(self.skills / "demo"), v2)
        backup = self.skills / ".slancha-backup" / "demo"
        self.assertEqual(self.tree(backup), V1)
        self.assertEqual((self.skills / ".slancha-backup" / ".gitignore").read_text(), "*\n")
        self.assertEqual(sorted(p.name for p in self.skills.iterdir()), [".slancha-backup", "demo"])
        self.assertIn(f"demo: up to date ({v2[:12]})", self.run_script("update"))

        v3 = self.api.publish("demo", {**V2, "SKILL.md": "Third.\n"})
        self.run_script("update")
        self.assertEqual(self.revision_of(self.skills / "demo"), v3)
        self.assertEqual(self.revision_of(backup), v2)             # one backup, the latest

    def test_dry_run_changes_nothing(self):
        v1 = self.install_v1()
        v2 = self.api.publish("demo", V2)
        before = len(self.api.requests)
        out = self.run_script("update", "--dry-run")
        self.assertIn(f"demo: would update {v1[:12]} -> {v2[:12]}", out)
        self.assertEqual(self.tree(self.skills / "demo"), V1)
        self.assertFalse([r for r in self.api.requests[before:] if "download" in r[1]])

    def test_refuses_local_edit_and_force_overrides(self):
        v1 = self.install_v1()
        edited = self.skills / "demo" / "SKILL.md"
        edited.write_text("My own version.\n")
        v2 = self.api.publish("demo", V2)
        out = self.run_script("update")
        self.assertIn(f"demo: kept {v1[:12]}, it has local changes (SKILL.md modified)", out)
        self.assertIn("--force", out)
        self.assertEqual(edited.read_text(), "My own version.\n")

        out = self.run_script("update", "--force")
        self.assertIn(f"demo: updated {v1[:12]} -> {v2[:12]}", out)
        self.assertEqual(self.tree(self.skills / "demo"), V2)
        self.assertEqual((self.skills / ".slancha-backup" / "demo" / "SKILL.md").read_text(),
                         "My own version.\n")

    def test_added_or_removed_file_is_a_local_edit(self):
        self.install_v1()
        (self.skills / "demo" / "mine.md").write_text("added\n")
        (self.skills / "demo" / "references" / "old.md").unlink()
        (self.skills / "demo" / "__pycache__").mkdir()                 # not an edit
        (self.skills / "demo" / "__pycache__" / "x.pyc").write_bytes(b"\0")
        self.api.publish("demo", V2)
        out = self.run_script("update")
        self.assertIn("local changes (mine.md added, references/old.md removed)", out)

    def test_project_skill_is_updated_where_it_lives(self):
        root = self.project / ".claude" / "skills"
        root.mkdir(parents=True)
        self.api.publish("demo", V1)
        v1 = self.api.published["demo"][0]
        data = skill_zip("demo", self.api.published["demo"][1], v1)
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:     # as a teammate would commit it
            bundle.extractall(root)
        v2 = self.api.publish("demo", V2)
        out = self.run_script("update")
        self.assertIn(f"demo: updated {v1[:12]} -> {v2[:12]}", out)
        self.assertEqual(self.revision_of(root / "demo"), v2)
        self.assertFalse((self.skills / "demo").exists())

    def test_skill_not_in_library_is_left_alone(self):
        self.install_v1()
        del self.api.published["demo"]
        self.assertIn("demo: not in your Slancha library; left as it is", self.run_script("update"))
        self.assertEqual(self.tree(self.skills / "demo"), V1)

    def test_pending_skill_is_not_installable(self):
        self.api.rows.append({"name": "fresh", "description": "", "revision": "", "active": False,
                              "updated_at": 1.0, "pending": True, "pending_kind": "new",
                              "uses_7d": 0})
        self.assertIn("fresh: not published yet", self.run_script("update", "fresh"))
        self.assertIn("nope: not in your Slancha library", self.run_script("update", "nope"))

    def test_unmanaged_directory_is_not_replaced_without_force(self):
        self.api.publish("demo", V1)
        mine = self.skills / "demo"
        mine.mkdir(parents=True)
        (mine / "SKILL.md").write_text("Hand written.\n")
        self.assertIn("was not installed by Slancha", self.run_script("update", "demo"))
        self.assertEqual((mine / "SKILL.md").read_text(), "Hand written.\n")
        self.assertIn("installed", self.run_script("update", "demo", "--force"))
        self.assertEqual(self.tree(mine), V1)

    def test_lists_the_library_when_nothing_is_installed(self):
        self.api.publish("demo", V1)
        out = self.run_script("update")
        self.assertIn("No Slancha skills are installed here.", out)
        self.assertIn("Also in your library: demo", out)

    def test_bad_arguments_answer_with_a_message(self):
        self.assertIn("is not a skill name", self.run_script("update", "../etc"))
        self.assertIn("Usage", self.run_script("update", "--bogus"))


class DownloadSafetyTests(PluginCase):
    """Each bad download is refused and leaves the installed revision exactly as it was."""

    def serve_v2(self, build):
        """Install v1, publish v2 and serve build(files, v2) -> (zip, headers) for it."""
        self.install_v1()
        v2 = self.api.publish("demo", V2)
        files = {p: d.encode() for p, d in V2.items()}
        blob, headers = build(files, v2)
        self.api.zip_for = lambda skill, revision: (blob, headers)
        out = self.run_script("update")
        self.assertEqual(self.tree(self.skills / "demo"), V1)
        self.assertFalse([p for p in self.tmp.rglob("*") if "evil" in p.name])
        self.assertFalse([p for p in self.skills.iterdir() if p.name.startswith(".slancha-tmp")])
        return out

    def refused(self, build, reason):
        out = self.serve_v2(build)
        self.assertIn("demo: refused the download, kept", out)
        self.assertIn(reason, out)

    @staticmethod
    def with_headers(blob, revision):
        return blob, {"X-Slancha-Revision": revision, "X-Slancha-Sha256": sha256(blob)}

    def extra_entry(self, name, data=b"evil\n", reason=""):
        self.refused(lambda files, v2: self.with_headers(
            skill_zip("demo", files, v2, extra=[(name, data)]), v2), reason)

    def test_zip_slip(self):
        self.extra_entry("demo/../evil.md", reason="'..' segment")

    def test_absolute_path(self):
        self.extra_entry("/tmp/evil.md", reason="is outside demo/")

    def test_entry_outside_the_skill(self):
        self.extra_entry("other/evil.md", reason="is outside demo/")

    def test_backslash_path(self):
        self.extra_entry("demo/..\\..\\evil.md", reason="not a plain relative path")

    def test_hidden_segment(self):
        self.extra_entry("demo/.git/evil.md", reason="hidden")

    def test_symlink_entry(self):
        self.extra_entry(entry("demo/evil-link", stat.S_IFLNK | 0o777), b"/etc/passwd",
                         reason="links and directories are refused")

    def test_directory_entry(self):
        self.extra_entry(entry("demo/evil-dir/", stat.S_IFDIR | 0o755), b"",
                         reason="links and directories are refused")

    def test_case_duplicate(self):
        self.extra_entry("demo/skill.md", b"evil\n", reason="appears twice")

    def test_too_many_files(self):
        many = {f"refs/{i:03}.md": b"x\n" for i in range(201)}
        many["SKILL.md"] = b"x\n"
        self.refused(lambda files, v2: self.with_headers(skill_zip("demo", many, v2), v2),
                     "entries; a skill has at most 200 files")

    def test_file_too_large(self):
        big = {"SKILL.md": b"x\n", "refs/big.md": b"x" * (256 * 1024 + 1)}
        self.refused(lambda files, v2: self.with_headers(skill_zip("demo", big, v2), v2),
                     "the limit is 262144")

    def test_skill_too_large(self):
        heavy = {f"refs/{i}.md": b"y" * (250 * 1024) for i in range(9)}
        heavy["SKILL.md"] = b"x\n"
        self.refused(lambda files, v2: self.with_headers(skill_zip("demo", heavy, v2), v2),
                     "more than 2097152 bytes")

    def test_zip_digest_mismatch(self):
        self.refused(lambda files, v2: (skill_zip("demo", files, v2), {
            "X-Slancha-Revision": v2, "X-Slancha-Sha256": "0" * 64}),
            "does not match its X-Slancha-Sha256")

    def test_revision_header_mismatch(self):
        self.refused(lambda files, v2: self.with_headers(skill_zip("demo", files, v2), "1" * 64),
                     "the download is revision 111111111111")

    def test_manifest_revision_mismatch(self):
        self.refused(lambda files, v2: self.with_headers(skill_zip("demo", files, "2" * 64), v2),
                     "says revision 222222222222")

    def test_manifest_names_another_skill(self):
        def build(files, v2):
            manifest = {"schema_version": "slancha/skill-download/v1", "skill": "other",
                        "revision": v2, "files": []}
            return self.with_headers(skill_zip("demo", files, v2, manifest=manifest), v2)
        self.refused(build, "names skill 'other'")

    def test_file_bytes_differ_from_manifest(self):
        def build(files, v2):
            listed = [{"path": p, "sha256": sha256(d), "size": len(d)} for p, d in files.items()]
            tampered = {**files, "SKILL.md": b"Tampered.\n"}
            manifest = {"schema_version": "slancha/skill-download/v1", "skill": "demo",
                        "revision": v2, "files": listed}
            return self.with_headers(skill_zip("demo", tampered, v2, manifest=manifest), v2)
        self.refused(build, "SKILL.md does not match its sha256")

    def test_file_missing_from_manifest(self):
        def build(files, v2):
            listed = [{"path": p, "sha256": sha256(d), "size": len(d)}
                      for p, d in files.items() if p != "scripts/run.py"]
            manifest = {"schema_version": "slancha/skill-download/v1", "skill": "demo",
                        "revision": v2, "files": listed}
            return self.with_headers(skill_zip("demo", files, v2, manifest=manifest), v2)
        self.refused(build, "disagree about scripts/run.py")

    def test_not_a_zip(self):
        self.refused(lambda files, v2: self.with_headers(b"<html>hello</html>", v2),
                     "not a zip file")


class NetworkTests(PluginCase):
    def test_redirect_is_refused_and_the_token_is_not_resent(self):
        elsewhere = FakeSlancha()
        self.addCleanup(elsewhere.close)
        self.api.publish("demo", V1)
        self.api.redirect = elsewhere.url
        out = self.run_script("update", "demo")
        self.assertIn("answered with a redirect (HTTP 302); not following it", out)
        self.assertEqual(elsewhere.requests, [])
        self.assertFalse((self.skills / "demo").exists())

    def test_api_down_exits_zero_with_a_message(self):
        env = {**self.env, "SLANCHA_API_URL": closed_port_url()}
        self.assertIn("Cannot reach 127.0.0.1", self.run_script("update", env=env))
        self.assertIn("Cannot reach 127.0.0.1", self.run_script("status", env=env))
        self.assertIn("Cannot reach 127.0.0.1", self.run_script("update", "demo", env=env))

    def test_only_https_is_accepted(self):
        for url in ("http://api.example.com", "ftp://127.0.0.1", "https://user:pw@api.example.com",
                    "https://api.example.com/?x=1"):
            env = {**self.env, "SLANCHA_API_URL": url}
            self.assertIn("must be an https:// URL", self.run_script("update", env=env), url)
        self.assertEqual(self.api.requests, [])

    def test_config_file_sets_the_api_url(self):
        self.api.publish("demo", V1)
        env = {k: v for k, v in self.env.items() if k != "SLANCHA_API_URL"}
        config = self.home / ".config" / "slancha"
        config.mkdir(parents=True)
        (config / "config.json").write_text(json.dumps({"api_url": self.api.url}))
        self.assertIn("demo: installed", self.run_script("update", "demo", env=env))

    def test_token_file(self):
        self.api.publish("demo", V1)
        env = {k: v for k, v in self.env.items() if k != "SLANCHA_TOKEN"}
        out = self.run_script("update", "demo", env=env)
        self.assertIn("No Slancha token yet", out)
        self.assertIn("umask 077; mkdir -p ~/.config/slancha; printf '%s' '<token>' > "
                      "~/.config/slancha/token", out)
        config = self.home / ".config" / "slancha"
        config.mkdir(parents=True)
        (config / "token").write_text(TOKEN + "\n")
        os.chmod(config / "token", 0o600)
        self.assertIn("demo: installed", self.run_script("update", "demo", env=env))
        out = self.run_script("status", env=env)
        self.assertIn("token from ~/.config/slancha/token", out)
        self.assertNotIn("Warning", out)
        os.chmod(config / "token", 0o644)
        self.assertIn("other users can read", self.run_script("status", env=env))

    def test_rejected_or_malformed_token(self):
        self.api.publish("demo", V1)
        env = {**self.env, "SLANCHA_TOKEN": "slancha_pat_revoked-token"}
        self.assertIn("did not accept the token (HTTP 401)", self.run_script("update", env=env))
        env = {**self.env, "SLANCHA_TOKEN": "not-a-token"}
        self.assertIn("is not a Slancha personal token", self.run_script("update", env=env))


class UsageTests(PluginCase):
    def setUp(self):
        super().setUp()
        self.v1 = self.install_v1()
        self.base = {"session_id": "session-3f1c", "cwd": str(self.project),
                     "transcript_path": "/private/transcript.jsonl", "permission_mode": "default"}

    def hook(self, env: dict | None = None, **event) -> str:
        return self.run_script("hook", stdin=json.dumps({**self.base, **event}), env=env)

    def skill_call(self, name="demo", event="PostToolUse", tool_use_id="toolu_01", **more):
        return self.hook(hook_event_name=event, tool_name="Skill", tool_use_id=tool_use_id,
                         prompt_id="prompt-1",
                         tool_input={"skill": name, "args": "SECRET-ARGUMENTS"},
                         tool_response={"success": True, "commandName": name,
                                        "content": "SECRET-RESPONSE"}, **more)

    def test_skill_tool_event_is_content_free(self):
        self.assertEqual(self.skill_call(), "")
        [event] = self.spool()
        self.assertEqual(set(event), {"id", "skill", "revision", "kind", "success", "session",
                                      "at"})
        self.assertEqual((event["skill"], event["revision"], event["kind"], event["success"]),
                         ("demo", self.v1, "invoked", True))
        self.assertEqual(event["session"], hashlib.sha256(b"session-3f1c").hexdigest())
        raw = (self.home / ".cache" / "slancha" / "usage.jsonl").read_text()
        for secret in ("SECRET", "session-3f1c", "transcript", "toolu_01", "prompt-1"):
            self.assertNotIn(secret, raw)
        spool = self.home / ".cache" / "slancha" / "usage.jsonl"
        self.assertEqual(stat.S_IMODE(spool.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(spool.parent.stat().st_mode) & 0o077, 0)

    def test_failed_call_and_typed_command(self):
        self.skill_call(event="PostToolUseFailure", tool_use_id="toolu_02", error="SECRET-ERROR")
        self.hook(hook_event_name="UserPromptExpansion", expansion_type="slash_command",
                  command_name="demo", command_args="SECRET", command_source="userSettings",
                  prompt="/demo SECRET", prompt_id="prompt-2")
        self.assertEqual([e["success"] for e in self.spool()], [False, None])
        self.assertNotIn("SECRET", json.dumps(self.spool()))

    def test_only_managed_skills_are_recorded(self):
        self.skill_call(name="someone-elses")
        self.skill_call(name="slancha:update")
        self.hook(hook_event_name="UserPromptExpansion", expansion_type="mcp_prompt",
                  command_name="demo", prompt_id="prompt-3")
        self.hook(hook_event_name="PostToolUse", tool_name="Read", tool_use_id="toolu_09",
                  tool_input={"file_path": str(self.skills / "demo" / "SKILL.md")})
        unmanaged = self.skills / "mine"
        unmanaged.mkdir()
        (unmanaged / "SKILL.md").write_text("Mine.\n")
        self.skill_call(name="mine")
        self.assertEqual(self.spool(), [])

    def test_frontmatter_name_maps_to_the_directory(self):
        (self.skills / "demo" / "SKILL.md").write_text(
            "---\nname: demo-alias\n---\nBody.\n")          # also a local edit
        self.skill_call(name="demo-alias")
        [event] = self.spool()
        self.assertEqual((event["skill"], event["revision"]), ("demo", "unknown"))

    def test_duplicate_hook_fires_are_one_event(self):
        self.skill_call()
        self.skill_call()
        first, second = self.spool()
        self.assertEqual(first["id"], second["id"])
        self.assertIn("sent 1 event", self.run_script("flush"))
        self.assertEqual(len(self.api.usage), 1)
        self.assertEqual(self.spool(), [])

    def test_offline_spool_then_flush(self):
        down = {**self.env, "SLANCHA_API_URL": closed_port_url()}
        self.skill_call(tool_use_id="toolu_a", env=down)
        self.skill_call(tool_use_id="toolu_b", env=down)
        self.assertIn("kept for later: Cannot reach", self.run_script("flush", env=down))
        self.assertEqual(len(self.spool()), 2)
        self.assertIn("2 events waiting to upload", self.run_script("status", env=down))

        out = self.run_script("flush")
        self.assertIn("sent 2 events", out)
        self.assertEqual({e["skill"] for e in self.api.usage.values()}, {"demo"})
        self.assertEqual(len(self.api.usage), 2)
        self.assertEqual(self.spool(), [])
        self.assertIn("nothing to upload", self.run_script("flush"))

    def test_lost_reply_is_retried_with_the_same_ids(self):
        self.skill_call(tool_use_id="toolu_a")
        self.skill_call(tool_use_id="toolu_b")
        self.api.store_then_fail = True
        self.assertIn("kept for later", self.run_script("flush"))
        self.assertEqual(len(self.spool()), 2)
        self.api.store_then_fail = False
        self.assertIn("sent 2 events", self.run_script("flush"))
        self.assertEqual(len(self.api.usage), 2)                     # stored once each

    def test_old_events_are_dropped(self):
        self.skill_call()
        [fresh] = self.spool()
        stale = {**fresh, "id": "00000000-0000-4000-8000-000000000000",
                 "at": fresh["at"] - 31 * 86400}
        with open(self.home / ".cache" / "slancha" / "usage.jsonl", "a") as handle:
            handle.write(json.dumps(stale) + "\n")
            handle.write("not json\n")
        self.assertIn("sent 1 event", self.run_script("flush"))
        self.assertEqual(list(self.api.usage), [fresh["id"]])
        self.assertEqual(self.spool(), [])

    def test_refused_batch_is_dropped_and_401_keeps_the_spool(self):
        self.skill_call()
        self.api.usage_status = 401
        self.assertIn("did not accept the token", self.run_script("flush"))
        self.assertEqual(len(self.spool()), 1)
        self.api.usage_status = 400
        self.assertIn("dropped 1 refused events (HTTP 400)", self.run_script("flush"))
        self.assertEqual(self.spool(), [])

    def test_stop_hook_uploads_in_the_background(self):
        self.skill_call()
        stop = {**self.base, "hook_event_name": "Stop", "stop_hook_active": False,
                "last_assistant_message": "SECRET"}
        started = time.monotonic()
        hook = subprocess.Popen([sys.executable, str(SCRIPT), "hook"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, env=self.env, start_new_session=True)
        out, _ = hook.communicate(json.dumps(stop).encode(), timeout=30)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(hook.pid, signal.SIGKILL)    # a host ending the hook's process group
        self.assertEqual((hook.returncode, out), (0, b""))
        self.assertLess(time.monotonic() - started, 5)
        deadline = time.monotonic() + 15
        while not self.api.usage and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(len(self.api.usage), 1)
        [event] = self.api.usage.values()
        self.assertNotIn("SECRET", json.dumps(event))

    def test_session_end_without_a_spool_does_nothing(self):
        before = len(self.api.requests)
        self.assertEqual(self.hook(hook_event_name="SessionEnd", reason="other"), "")
        time.sleep(0.5)
        self.assertEqual(len(self.api.requests), before)

    def test_hooks_never_fail_and_print_nothing(self):
        for stdin in ("", "not json", "[]", "null", '{"hook_event_name": 5}'):
            self.assertEqual(self.run_script("hook", stdin=stdin), "")
        down = {**self.env, "SLANCHA_API_URL": closed_port_url()}
        self.assertEqual(self.hook(hook_event_name="SessionStart", source="startup", env=down), "")
        broken = {**self.env, "SLANCHA_API_URL": "http://example.com"}
        self.assertEqual(self.skill_call(env=broken), "")        # recorded; the upload says why not

    def test_session_start_notice_once_a_day(self):
        self.api.publish("demo", V2)
        out = self.hook(hook_event_name="SessionStart", source="startup")
        notice = json.loads(out)
        self.assertEqual(set(notice), {"systemMessage"})
        self.assertIn("updates available for demo", notice["systemMessage"])
        self.assertIn("/slancha:update", notice["systemMessage"])
        self.assertEqual(self.hook(hook_event_name="SessionStart", source="startup"), "")

    def test_session_start_is_quiet_when_up_to_date_or_without_token(self):
        self.assertEqual(self.hook(hook_event_name="SessionStart", source="startup"), "")
        stamp = self.home / ".cache" / "slancha" / "update-check.stamp"
        stamp.unlink()
        self.api.publish("demo", V2)
        no_token = {k: v for k, v in self.env.items() if k != "SLANCHA_TOKEN"}
        self.assertEqual(self.hook(hook_event_name="SessionStart", source="startup",
                                   env=no_token), "")

    def test_status(self):
        v2 = self.api.publish("demo", V2)
        self.skill_call()
        out = self.run_script("status")
        self.assertIn(f"demo  {self.v1[:12]}  ~/.claude/skills  update available: {v2[:12]}", out)
        self.assertIn("Usage: 1 event waiting to upload.", out)


if __name__ == "__main__":
    unittest.main()
