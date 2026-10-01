"""`slancha auth` (the device authorization grant) against the fake API, and `slancha init` against
fake `claude`, `git` and `uv` commands. The script runs in a subprocess with HOME pointed at a temp
directory, as in test_slancha.py.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from fake_api import DEVICE_TOKEN, TOKEN, TRACING, FakeSlancha  # noqa: E402

SCRIPT = TESTS.parent / "scripts" / "slancha.py"
LANGFUSE = "langfuse-observability@langfuse-observability"
PIN = "b5211009698fdfe79c6bbe90d6901ad255ea65d3"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class SetupCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.config = self.home / ".config" / "slancha"
        self.api = FakeSlancha()
        self.addCleanup(self.api.close)
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "SLANCHA_API_URL": self.api.url}

    def run_script(self, *args, env: dict | None = None, code: int = 0) -> str:
        result = subprocess.run([sys.executable, str(SCRIPT), *args], text=True,
                                stdin=subprocess.DEVNULL, capture_output=True,
                                env=env or self.env, timeout=60)
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, code, out)
        for secret in (TOKEN, DEVICE_TOKEN, TRACING["secret_key"]):
            self.assertNotIn(secret, out)
        return out


class AuthTests(SetupCase):
    def test_pending_then_approved(self):
        self.config.mkdir(parents=True)
        (self.config / "config.json").write_text('{"keep": 1}')
        out = self.run_script("auth", "--no-browser")
        self.assertIn("WDJB-MJHT", out)
        self.assertIn(f"{self.api.url}/cli?code=WDJB-MJHT", out)
        self.assertIn("An admin of your Slancha org approves", out)
        self.assertIn("Approved for org org-1.", out)
        self.assertEqual(self.api.polls, 2)                      # one pending, then approved
        self.assertTrue(self.api.device_bodies[0]["client"].startswith("slancha CLI on "))
        self.assertEqual(self.api.device_bodies[1], {"device_code": "device-code-1"})
        self.assertTrue(all(auth is None for _, _, auth in self.api.requests))

        self.assertEqual((self.config / "token").read_text(), DEVICE_TOKEN)
        self.assertEqual(json.loads((self.config / "config.json").read_text()),
                         {"keep": 1, "api_url": self.api.url})
        self.assertEqual(json.loads((self.config / "tracing.json").read_text()), TRACING)
        self.assertEqual(mode(self.config), 0o700)
        for name in ("token", "config.json", "tracing.json"):
            self.assertEqual(mode(self.config / name), 0o600, name)
        self.assertEqual(sorted(p.name for p in self.config.iterdir()),
                         ["config.json", "token", "tracing.json"])       # no temp files left

        # the plugin reads the saved token and API
        env = {k: v for k, v in self.env.items() if k != "SLANCHA_API_URL"}
        self.assertIn("token from ~/.config/slancha/token", self.run_script("status", env=env))

    def test_api_flag_is_saved(self):
        env = {k: v for k, v in self.env.items() if k != "SLANCHA_API_URL"}
        self.run_script("auth", "--no-browser", "--api", self.api.url + "/", env=env)
        self.assertEqual(json.loads((self.config / "config.json").read_text()),
                         {"api_url": self.api.url})
        out = self.run_script("auth", "--api", "http://example.test", env=env, code=1)
        self.assertIn("--api must be an https:// URL", out)

    def test_an_api_that_is_not_slancha_names_the_setting_to_fix(self):
        self.api.not_slancha = True
        out = self.run_script("auth", "--no-browser", code=1)
        self.assertIn("Slancha answered HTTP 429.", out)
        self.assertIn("your Slancha API? The Claude Code page of your Slancha console", out)
        self.assertIn("or run slancha auth --api <url>", out)
        self.assertFalse((self.config / "token").exists())

    def test_expired(self):
        self.api.outcome = "expired"
        out = self.run_script("auth", "--no-browser", code=1)
        self.assertIn("The code expired", out)
        self.assertIn("run slancha auth again", out)
        self.assertFalse((self.config / "token").exists())

    def test_no_tracing_keys(self):
        self.config.mkdir(parents=True)
        (self.config / "tracing.json").write_text(json.dumps(TRACING))
        self.api.tracing = None
        self.api.pending = 0
        out = self.run_script("auth", "--no-browser")
        self.assertIn("sent no tracing keys", out)
        self.assertFalse((self.config / "tracing.json").exists())   # from an earlier sign-in
        self.assertEqual((self.config / "token").read_text(), DEVICE_TOKEN)

    def test_ctrl_c_stops_cleanly(self):
        self.api.outcome = "pending"
        proc = subprocess.Popen([sys.executable, str(SCRIPT), "auth", "--no-browser"], text=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=self.env)
        self.addCleanup(proc.kill)
        seen = ""
        while "Waiting" not in seen:
            line = proc.stdout.readline()
            self.assertTrue(line, seen)
            seen += line
        proc.send_signal(signal.SIGINT)
        rest, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 130, seen + rest)
        self.assertIn("Stopped. Nothing was saved.", rest)
        self.assertNotIn("Traceback", rest)
        self.assertFalse((self.config / "token").exists())


class InitCase(SetupCase):
    """Fake claude, git and uv on PATH, a token, tracing keys, and a settings.json to merge into."""
    def setUp(self):
        super().setUp()
        self.bin = self.home.parent / "bin"
        self.bin.mkdir()
        for tool in ("claude", "git", "uv"):
            self.tool(tool)
        self.env.update(PATH=str(self.bin), SLANCHA_TOKEN=TOKEN)
        self.config.mkdir(parents=True)
        (self.config / "tracing.json").write_text(json.dumps(TRACING))
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps({
            "model": "opus",
            "env": {"FOO": "1", "LANGFUSE_PUBLIC_KEY": "old", "LANGFUSE_HOST": "https://old.test"},
            "pluginConfigs": {"other@market": {"options": {"a": 1}},
                              LANGFUSE: {"options": {"CC_LANGFUSE_MAX_CHARS": 5}}}}))
        self.settings.chmod(0o644)

    def tool(self, name: str) -> None:
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(TESTS)!r})\n"
                        f"import fake_tools\nfake_tools.main({name!r})\n")
        path.chmod(0o755)

    def calls(self) -> list[list[str]]:
        log = self.home / "tools.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def changing(self) -> list[list[str]]:
        """The tool calls that change something."""
        return [c for c in self.calls() if c[1:3] not in (["plugin", "list"],
                                                         ["plugin", "marketplace"])
                or c[3:4] == ["add"]
                if c[:1] != ["uv"] and "rev-parse" not in c and "cat-file" not in c]


class InitTests(InitCase):
    def test_configures_claude_code_and_is_idempotent(self):
        out = self.run_script("init")
        repo = str(self.home / ".local" / "share" / "slancha" / "langfuse-observability")
        self.assertEqual(self.changing(), [
            ["git", "clone", "--quiet", "https://github.com/langfuse/claude-observability-plugin",
             repo],
            ["git", "-C", repo, "checkout", "--quiet", "--detach", PIN],
            ["claude", "plugin", "marketplace", "add", repo],
            ["claude", "plugin", "install", LANGFUSE, "--scope", "user"],
            ["claude", "plugin", "marketplace", "add", "SlanchaAI/slancha-claude-plugin"],
            ["claude", "plugin", "install", "slancha@slancha", "--scope", "user"]])
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["env"], {
            "FOO": "1", "CC_LANGFUSE_PUBLIC_KEY": TRACING["public_key"],
            "CC_LANGFUSE_SECRET_KEY": TRACING["secret_key"],
            "CC_LANGFUSE_BASE_URL": TRACING["host"]})
        self.assertEqual(settings["pluginConfigs"], {
            "other@market": {"options": {"a": 1}},
            LANGFUSE: {"options": {"CC_LANGFUSE_MAX_CHARS": 5, "CC_LANGFUSE_SKILL_TAGS": True,
                                   "CC_LANGFUSE_CAPTURE_IMAGES": False}}})
        # written by `claude plugin install`, kept by the merge that follows it
        self.assertEqual(settings["enabledPlugins"], {LANGFUSE: True, "slancha@slancha": True})
        self.assertNotIn("hooks", settings)
        self.assertEqual(mode(self.settings), 0o600)
        self.assertIn("removed env LANGFUSE_PUBLIC_KEY, LANGFUSE_HOST", out)
        self.assertIn("mode 0600", out)
        self.assertIn("Restart Claude Code", out)

        (self.home / "tools.log").unlink()
        before = self.settings.read_bytes()
        out = self.run_script("init")
        self.assertEqual(self.changing(), [])
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertIn("already configured", out)

    def test_dry_run_changes_nothing(self):
        before = self.settings.read_bytes()
        out = self.run_script("init", "--dry-run")
        self.assertEqual(self.changing(), [])
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(mode(self.settings), 0o644)
        self.assertFalse((self.home / ".local").exists())
        self.assertIn("would run: git clone", out)
        self.assertIn("would run: claude plugin install slancha@slancha --scope user", out)
        self.assertIn("would remove env LANGFUSE_PUBLIC_KEY, LANGFUSE_HOST", out)
        self.assertIn("would write it atomically with mode 0600", out)

    def test_claude_config_dir(self):
        alt = self.home / "alt-claude"
        self.env["CLAUDE_CONFIG_DIR"] = str(alt)
        self.run_script("init")
        settings = json.loads((alt / "settings.json").read_text())
        self.assertEqual(settings["env"]["CC_LANGFUSE_BASE_URL"], TRACING["host"])
        self.assertEqual(mode(alt / "settings.json"), 0o600)
        self.assertIn("FOO", json.loads(self.settings.read_text())["env"])    # untouched

    def test_missing_prerequisites(self):
        for name in ("claude", "git", "uv"):
            (self.bin / name).unlink()
        out = self.run_script("init", code=1)
        for tool in ("`claude`", "`git`", "`uv`"):
            self.assertIn(f"{tool} is not on PATH", out)
        self.tool("uv")
        self.env["FAKE_UV_NO_PYTHON"] = "1"
        out = self.run_script("init", code=1)
        self.assertIn("uv python install 3.12", out)
        self.assertNotIn("`uv` is not on PATH", out)

    def test_check_needs_no_sign_in_and_changes_nothing(self):
        del self.env["SLANCHA_TOKEN"]
        (self.config / "tracing.json").unlink()
        self.assertIn("This machine has what slancha init needs", self.run_script("init", "--check"))
        (self.bin / "git").unlink()
        out = self.run_script("init", "--check", code=1)
        self.assertIn("Slancha needs, on this machine:", out)
        self.assertIn("`git` is not on PATH", out)
        self.assertEqual(self.changing(), [])
        self.assertEqual(json.loads(self.settings.read_text())["model"], "opus")
        self.assertFalse((self.config / "token").exists())

    def test_needs_auth_first(self):
        del self.env["SLANCHA_TOKEN"]
        self.assertIn("run slancha auth first", self.run_script("init", code=1))
        self.env["SLANCHA_TOKEN"] = TOKEN
        (self.config / "tracing.json").unlink()
        self.assertIn("run slancha auth first", self.run_script("init", code=1))
        self.assertEqual(self.calls(), [])

    def test_checkout_that_misses_the_pin_fails(self):
        self.env["FAKE_GIT_BAD_CHECKOUT"] = "1"
        out = self.run_script("init", code=1)
        self.assertIn(f"is not at {PIN}", out)
        self.assertNotIn(["claude", "plugin", "install", LANGFUSE, "--scope", "user"],
                         self.calls())

    def test_unpinned_langfuse_marketplace_is_refused(self):
        (self.home / "fake-claude.json").write_text(json.dumps({
            "markets": {"langfuse-observability": "/elsewhere"}, "plugins": [LANGFUSE]}))
        out = self.run_script("init", code=1)
        self.assertIn("claude plugin marketplace remove langfuse-observability", out)

    def test_manual_langfuse_hook_is_reported_not_added(self):
        hooks = {"Stop": [{"hooks": [{"type": "command",
                                      "command": "uv run ~/langfuse_hook.py"}]}]}
        self.settings.write_text(json.dumps({"hooks": hooks}))
        out = self.run_script("init")
        self.assertIn("hook that runs Langfuse", out)
        self.assertEqual(json.loads(self.settings.read_text())["hooks"], hooks)

    def test_marketplace_name_matches_the_repo(self):
        # install.sh ships slancha.py alone, so the name is a constant in it
        text = SCRIPT.read_text()
        doc = json.loads((TESTS.parent / ".claude-plugin" / "marketplace.json").read_text())
        self.assertIn(f'\nSLANCHA_MARKETPLACE = "{doc["name"]}"', text)
        self.assertIn(f'\nLANGFUSE_COMMIT = "{PIN}"', text)


class UninstallTests(InitCase):
    def installed(self) -> dict:
        """A machine after install.sh: init has run, and auth's files and the CLI are in place."""
        self.run_script("init")
        (self.config / "token").write_text(TOKEN)
        (self.config / "config.json").write_text('{"api_url": "https://api.example.test"}')
        share = self.home / ".local" / "share" / "slancha"
        share.mkdir(parents=True, exist_ok=True)
        (share / "slancha.py").write_text("# the CLI\n")
        command = self.home / ".local" / "bin" / "slancha"
        command.parent.mkdir(parents=True)
        command.write_text(f'#!/bin/sh\nexec python3 "{share}/slancha.py" "$@"\n')
        cache = self.home / ".cache" / "slancha"
        cache.mkdir(parents=True)
        (cache / "usage.jsonl").write_text('{"id": "e1"}\n{"id": "e2"}\n')
        skill = self.home / ".claude" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "slancha-manifest.json").write_text("{}")
        return {"share": share, "command": command, "cache": cache, "skill": skill}

    def test_undoes_init_and_auth_and_keeps_what_is_not_its_own(self):
        paths = self.installed()
        out = self.run_script("uninstall")
        self.assertEqual(json.loads((self.home / "fake-claude.json").read_text()),
                         {"markets": {}, "plugins": []})
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["env"], {"FOO": "1"})          # init had removed the SDK names
        self.assertEqual(settings["pluginConfigs"], {"other@market": {"options": {"a": 1}},
                                                     LANGFUSE: {"options": {"CC_LANGFUSE_MAX_CHARS": 5}}})
        self.assertEqual(settings.get("enabledPlugins"), {})
        for gone in (self.config, paths["cache"], paths["share"], paths["command"]):
            self.assertFalse(gone.exists(), gone)
        self.assertTrue((paths["skill"] / "slancha-manifest.json").exists())
        self.assertIn("2 usage events not yet sent to Slancha were dropped", out)
        self.assertIn("stay in ~/.claude/skills and no longer update: demo", out)
        self.assertIn("Revoke the personal token in the Slancha console", out)
        again = self.run_script("uninstall")                     # nothing left to do
        self.assertIn("nothing of Slancha's in it", again)
        self.assertNotIn("removing", again)

    def test_dry_run_changes_nothing(self):
        paths = self.installed()
        before = self.settings.read_text()
        out = self.run_script("uninstall", "--dry-run")
        self.assertIn("would run: claude plugin uninstall slancha@slancha --scope user", out)
        self.assertIn("would remove env CC_LANGFUSE_PUBLIC_KEY, CC_LANGFUSE_SECRET_KEY, "
                      "CC_LANGFUSE_BASE_URL", out)
        self.assertEqual(self.settings.read_text(), before)
        for kept in (self.config / "token", paths["cache"], paths["share"], paths["command"]):
            self.assertTrue(kept.exists(), kept)
        self.assertEqual(len(json.loads((self.home / "fake-claude.json").read_text())["plugins"]), 2)

    def test_a_langfuse_key_someone_else_set_stays(self):
        self.installed()
        settings = json.loads(self.settings.read_text())
        settings["env"]["CC_LANGFUSE_BASE_URL"] = "https://their-own.example.test"
        self.settings.write_text(json.dumps(settings))
        out = self.run_script("uninstall")
        env = json.loads(self.settings.read_text())["env"]
        self.assertEqual(env, {"FOO": "1", "CC_LANGFUSE_BASE_URL": "https://their-own.example.test"})
        self.assertIn("keep env CC_LANGFUSE_BASE_URL: not the values slancha init wrote", out)

    def test_a_command_named_slancha_that_is_not_ours_stays(self):
        paths = self.installed()
        paths["command"].write_text("#!/bin/sh\necho someone else's slancha\n")
        self.run_script("uninstall")
        self.assertTrue(paths["command"].exists())


class ReleaseTests(unittest.TestCase):
    def test_the_plugin_and_the_cli_carry_one_version(self):
        # Claude Code updates an installed plugin only when plugin.json's version changes, and
        # scripts/release.sh sets both; a hand edit to one would ship two versions.
        manifest = json.loads((TESTS.parent / ".claude-plugin" / "plugin.json").read_text())
        self.assertIn(f'\nVERSION = "{manifest["version"]}"\n', SCRIPT.read_text())

    def test_the_released_installer_takes_no_pin_from_the_environment(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed here; CI runs this")
        result = subprocess.run(["bash", str(TESTS.parent / "scripts" / "release.sh"), "installer"],
                                cwd=TESTS.parent, capture_output=True, text=True, timeout=60)
        if result.returncode and "commit scripts/slancha.py" in result.stderr:
            self.skipTest("uncommitted changes: the installer names a commit")
        if result.returncode and "not a git repository" in result.stderr:
            self.skipTest("not a git checkout")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("PLACEHOLDER", result.stdout)
        self.assertRegex(result.stdout, r'\nSLANCHA_REF="[0-9a-f]{40}"\n')
        self.assertRegex(result.stdout, r'\nSLANCHA_SHA256="[0-9a-f]{64}"\n')


if __name__ == "__main__":
    unittest.main()
