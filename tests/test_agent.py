"""The commands for a person or a coding agent with an agent token (slancha auth --manage):
whoami, skills, skill, upload, pending, review, jobs, job, scan, draft, outcomes and criteria,
against the fake API. Each runs scripts/slancha.py in a subprocess with HOME in a temp directory,
and is checked as people read it, as --json, and by its exit code: 0 done, 1 an error or a
refusal, 2 a usage error, 3 the acceptance criteria are not met.
"""
from __future__ import annotations

import difflib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
from fake_api import AGENT_TOKEN, TOKEN, FakeSlancha  # noqa: E402

SCRIPT = TESTS.parent / "scripts" / "slancha.py"
SKILL = {"SKILL.md": "---\nname: demo\ndescription: A demo skill.\n---\n## When to use\n"
                     "Always, for the demo.\n",
         "scripts/run.sh": "echo hi\n"}
RULES = [{"type": "includes", "path": "SKILL.md", "text": "## When to use"},
         {"type": "file", "path": "scripts/run.sh"}]


class AgentCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.api = FakeSlancha()
        self.addCleanup(self.api.close)
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "SLANCHA_API_URL": self.api.url, "SLANCHA_TOKEN": AGENT_TOKEN}
        self.v1 = self.api.publish("demo", {**SKILL, "SKILL.md": SKILL["SKILL.md"].replace(
            "Always", "Sometimes")})

    def slancha(self, *args, code: int = 0, token: str | None = None) -> str:
        env = {**self.env, "SLANCHA_TOKEN": token or AGENT_TOKEN}
        result = subprocess.run([sys.executable, str(SCRIPT), *args], text=True,
                                stdin=subprocess.DEVNULL, capture_output=True, env=env,
                                timeout=120)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        for secret in (TOKEN, AGENT_TOKEN):
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.stderr = result.stderr
        return result.stdout

    def as_json(self, *args, code: int = 0, token: str | None = None):
        return json.loads(self.slancha(*args, "--json", code=code, token=token))

    def package(self, files: dict | None = None, name: str = "demo") -> str:
        root = self.tmp / "src" / name
        for path, text in (files or SKILL).items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text)
        return str(root)

    def sent(self, path: str) -> dict:
        return [body for p, body in self.api.bodies if p == path][-1]


class TokenTests(AgentCase):
    def test_whoami(self):
        out = self.slancha("whoami")
        self.assertIn("dev@example.test: proposer in Example Org", out)
        self.assertIn("Token: skills:read, usage:write, manage; expires ", out)
        self.assertIn("(in 30 days)", out)
        doc = self.as_json("whoami")
        self.assertIn("manage", doc["capabilities"])
        self.assertIsInstance(doc["expires_at"], float)
        out = self.slancha("whoami", token=TOKEN)
        self.assertIn("viewer", out)
        self.assertIn("Token: skills:read, usage:write; does not expire.", out)
        self.assertIn("slancha auth --manage signs in an agent token", out)

    def test_a_plain_token_is_pointed_at_auth_manage(self):
        self.assertIn("demo", self.slancha("skills", token=TOKEN))
        out = self.slancha("skill", "demo", code=1, token=TOKEN)
        self.assertIn("HTTP 403: a token cannot do this", out)
        self.assertIn("This needs an agent token: run slancha auth --manage.", out)
        doc = self.as_json("pending", code=1, token=TOKEN)
        self.assertEqual(doc["status"], 403)
        self.assertIn("slancha auth --manage", doc["error"])

    def test_an_expired_token_says_so(self):
        self.api.expired = True
        self.assertIn("Your Slancha token expired; run slancha auth",
                      self.slancha("whoami", code=1))
        self.assertEqual(self.as_json("skills", code=1)["status"], 401)

    def test_usage_errors_exit_2_and_there_is_no_approval(self):
        for args in (["skill", "../etc"], ["scan", "demo", "--revision", "abc"],
                     ["outcomes", "demo", "--days", "91"], ["criteria", "demo", "--check", "x",
                                                            "--revision", "0" * 64],
                     ["approve", "demo"], ["reject", "demo"], ["rollback", "demo"],
                     ["criteria", "demo", "set"]):
            self.slancha(*args, code=2)
        self.assertEqual(self.api.requests, [])
        out = self.slancha("--help")
        self.assertIn("3 the acceptance criteria are not", out)
        self.assertIn("people\nin the Slancha console; no command here does them", out)


class LibraryTests(AgentCase):
    def test_skills(self):
        self.api.rows.append({"name": "fresh", "description": "", "revision": "", "active": False,
                              "updated_at": 1.0, "pending": True, "pending_kind": "creation",
                              "uses_7d": 0})
        out = self.slancha("skills")
        self.assertIn(f"  demo  serves {self.v1[:12]}  0 uses in 7 days  demo skill", out)
        self.assertIn("  fresh  not served, creation waiting for review  0 uses", out)
        self.assertEqual([r["name"] for r in self.as_json("skills")], ["demo", "fresh"])

    def test_skill_shows_versions_and_criteria(self):
        out = self.slancha("skill", "demo")
        self.assertIn(f"demo: serves {self.v1[:12]}", out)
        self.assertIn(f"approve  absent -> {self.v1[:12]}  by boss@example.test", out)
        self.assertIn("Acceptance criteria: none (an admin sets them in the console).", out)
        self.api.criteria["demo"] = RULES + [{"type": "includes", "path": "SKILL.md",
                                             "text": "## Examples"}]
        out = self.slancha("skill", "demo")
        self.assertIn("Acceptance criteria: 3 rules, set by boss@example.test", out)
        self.assertIn(f"Served {self.v1[:12]}: fails 1 of 3", out)
        self.assertIn('  ✓ SKILL.md must include "## When to use"', out)
        self.assertIn('  ✗ SKILL.md must include "## Examples": SKILL.md does not include '
                      '"## Examples"', out)
        doc = self.as_json("skill", "demo")
        self.assertEqual(doc["versions"]["active"], self.v1)
        self.assertIs(doc["criteria"]["served"]["ok"], False)
        self.assertIn("no skill named 'nope'", self.slancha("skill", "nope", code=1))


class CriteriaAndUploadTests(AgentCase):
    def setUp(self):
        super().setUp()
        self.api.criteria["demo"] = list(RULES)

    def test_check_a_package_before_uploading_it(self):
        good = self.package()
        out = self.slancha("criteria", "demo", "--check", good)
        self.assertIn(f"{good} against the acceptance criteria of demo: meets all 2", out)
        self.assertIn('  ✓ SKILL.md must include "## When to use"', out)
        self.assertEqual(self.sent("/api/skills/demo/criteria/check"),
                         {"files": [{"path": "SKILL.md", "content": SKILL["SKILL.md"]},
                                    {"path": "scripts/run.sh", "content": "echo hi\n"}]})
        bad = self.package({"SKILL.md": SKILL["SKILL.md"]}, name="bad")
        out = self.slancha("criteria", "demo", "--check", bad, code=3)
        self.assertIn("fails 1 of 2", out)
        self.assertIn("  ✗ scripts/run.sh must exist: scripts/run.sh is missing", out)
        doc = self.as_json("criteria", "demo", "--check", bad, code=3)
        self.assertEqual((doc["ok"], doc["failures"]),
                         (False, ["scripts/run.sh must exist: scripts/run.sh is missing"]))
        self.assertIn("meets all 2", self.slancha("criteria", "demo", "--revision", self.v1))
        self.assertEqual(self.sent("/api/skills/demo/criteria/check"), {"revision": self.v1})
        self.assertEqual(self.api.proposals, {})                    # nothing stored
        out = self.slancha("criteria", "demo")
        self.assertIn("Acceptance criteria: 2 rules", out)
        self.assertIn(f"Served {self.v1[:12]}: meets all 2", out)

    def test_upload_that_fails_the_criteria_is_stored_and_exits_3(self):
        out = self.slancha("upload", self.package({"SKILL.md": SKILL["SKILL.md"]}), code=3)
        record = self.api.proposals["demo"]
        self.assertIn(f"Uploaded demo: revision {record['revision'][:12]}, proposal "
                      f"{record['id']} (update). It waits for a person to review it", out)
        self.assertIn("Gate: blocked\n  blocked: acceptance criteria: scripts/run.sh must exist: "
                      "scripts/run.sh is missing", out)
        self.assertIn("Acceptance criteria: fails 1 of 2", out)
        self.assertIn("  ✗ scripts/run.sh must exist: scripts/run.sh is missing", out)
        self.assertIn("Security scan: job job_1; slancha job job_1 --wait", out)
        self.assertIn("The change is stored but cannot be approved", out)
        self.assertIn("slancha criteria <skill> --check <path>", out)
        self.assertIn("already waiting for review", self.slancha("upload", self.package(), code=1))

    def test_upload_json_leaves_out_what_the_server_refuses(self):
        root = Path(self.package())
        (root / ".hidden").write_text("x")
        (root / "slancha-manifest.json").write_text("{}")         # an installed copy's
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "x.pyc").write_bytes(b"\0")
        doc = self.as_json("upload", str(root), "--name", "demo")
        self.assertEqual(doc["criteria"]["ok"], True)
        self.assertEqual(doc["scan_job"], "job_1")
        self.assertIn("Left out: __pycache__/, .hidden, slancha-manifest.json", self.stderr)
        body = self.sent("/api/skills/upload")
        self.assertEqual(body["name"], "demo")
        self.assertEqual([f["path"] for f in body["files"]], ["SKILL.md", "scripts/run.sh"])

    def test_upload_a_zip(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            for path, text in SKILL.items():
                bundle.writestr(f"demo/{path}", text)
        archive = self.tmp / "demo.zip"
        archive.write_bytes(buffer.getvalue())
        self.assertIn("Acceptance criteria: meets all 2", self.slancha("upload", str(archive)))
        self.assertEqual(set(self.sent("/api/skills/upload")), {"archive"})
        self.assertEqual(self.api.proposals["demo"]["files"], SKILL)

    def test_what_upload_refuses_before_sending(self):
        root = Path(self.package())
        (root / "logo.md").write_bytes(b"\xff\xfe")
        self.assertIn("logo.md is not UTF-8 text", self.slancha("upload", str(root), code=1))
        (root / "logo.md").unlink()
        (root / "link.md").symlink_to(root / "SKILL.md")
        self.assertIn("link.md is a link", self.slancha("upload", str(root), code=1))
        (root / "link.md").unlink()
        (root / "big.md").write_text("x" * (256 * 1024 + 1))
        self.assertIn("is too large at big.md", self.slancha("upload", str(root), code=1))
        self.assertIn("neither a directory nor a .zip", self.slancha("upload", "/nope", code=1))
        self.assertEqual(self.api.bodies, [])

    def test_a_409_for_unmet_criteria_exits_3(self):
        detail = "acceptance criteria not met: scripts/run.sh must exist: scripts/run.sh is missing"
        self.api.refuse["/api/skills/upload"] = (409, detail)
        self.assertIn(detail, self.slancha("upload", self.package(), code=3))
        self.assertEqual(self.as_json("upload", self.package(), code=3),
                         {"error": f"Slancha answered HTTP 409: {detail}", "status": 409})
        self.api.refuse["/api/skills/upload"] = (409, "a change to 'demo' is already waiting")
        self.slancha("upload", self.package(), code=1)


class ReviewTests(AgentCase):
    def setUp(self):
        super().setUp()
        self.slancha("upload", self.package())
        self.record = self.api.proposals["demo"]

    def test_pending(self):
        out = self.slancha("pending")
        self.assertIn(f"  demo  update  {self.v1[:12]} -> {self.record['revision'][:12]}  proposal "
                      f"{self.record['id']}  by dev@example.test", out)
        out = self.slancha("pending", "demo")      # the fake refuses without comparison_paging
        self.assertIn(f"demo: update {self.v1[:12]} -> {self.record['revision'][:12]}", out)
        self.assertIn("Files: SKILL.md modified", out)
        self.assertIn("Security scan: no clean security scan of this revision yet", out)
        self.assertEqual(self.as_json("pending", "demo")["id"], self.record["id"])
        self.assertEqual([r["skill"] for r in self.as_json("pending")], ["demo"])
        self.api.criteria["demo"] = [{"type": "file", "path": "CHANGELOG"}]
        out = self.slancha("pending", "demo")
        self.assertIn("Gate: blocked", out)
        self.assertIn("  blocked: acceptance criteria: CHANGELOG must exist: CHANGELOG is missing",
                      out)
        self.api.proposals.clear()
        self.assertIn("Nothing is waiting for review.", self.slancha("pending"))
        self.assertIn("no pending change for 'demo'", self.slancha("pending", "demo", code=1))

    def test_review_reads_every_page_of_every_diff(self):
        before = SKILL["SKILL.md"].replace("Always", "Sometimes").splitlines()
        whole = "\n".join(difflib.unified_diff(before, SKILL["SKILL.md"].splitlines(),
                                               fromfile="SKILL.md (base)",
                                               tofile="SKILL.md (proposed)", lineterm=""))
        before = len(self.api.requests)
        out = self.slancha("review", "demo")
        self.assertIn("== SKILL.md\n" + whole + "\n", out)
        self.assertIn("Evidence:\n  {\n    \"schema_version\": \"slancha/upload/v1\"\n  }", out)
        self.assertIn("read-only: a person approves or rejects the change in the Slancha console",
                      out)
        pages = [p for _, p, _ in self.api.requests if "/comparison?" in p]
        self.assertGreater(len(pages), 2)                   # 40 characters a page
        self.assertTrue(all("view=diff" in p and f"proposal_id={self.record['id']}" in p
                            for p in pages))
        doc = self.as_json("review", "demo")
        self.assertEqual(doc["diffs"], {"file:SKILL.md": whole})
        self.assertEqual(doc["evidence"]["proposal_id"], self.record["id"])
        self.assertEqual(doc["card"]["skill"], "demo")
        self.assertFalse([r for r in self.api.requests[before:] if r[0] != "GET"])   # read-only
        self.assertIn("no pending change for 'other'", self.slancha("review", "other", code=1))


class JobTests(AgentCase):
    def test_jobs_and_one_job(self):
        self.api.start_job("scan", "demo", self.v1)
        self.api.start_job("draft", "other", "0" * 64)
        out = self.slancha("jobs")
        self.assertIn(f"  job_1  scan  demo  {self.v1[:12]}  queued", out)
        self.assertIn("  job_2  draft  other", out)
        self.assertEqual([j["id"] for j in self.as_json("jobs", "--skill", "demo")], ["job_1"])
        self.assertIn("No jobs yet.", self.slancha("jobs", "--skill", "nope"))
        self.assertIn("Job job_1  scan  demo", self.slancha("job", "job_1"))   # running
        doc = self.as_json("job", "job_1", "--wait")               # ends on this read
        self.assertEqual((doc["status"], doc["result"]), ("succeeded", {"ok": True}))
        self.api.job_end = "failed"
        self.slancha("job", "job_2")
        self.assertIn("failed", self.slancha("job", "job_2", "--wait", code=1))
        self.assertEqual(self.as_json("job", "job_1", "--cancel")["status"], "cancelled")
        self.assertIn(("POST", "/api/jobs/job_1/cancel", f"Bearer {AGENT_TOKEN}"),
                      self.api.requests)
        self.assertIn("no such job", self.slancha("job", "job_9", code=1))

    def test_scan_and_wait(self):
        out = self.slancha("scan", "demo", "--revision", self.v1)
        self.assertEqual(self.sent(f"/api/skills/demo/scan"), {"revision": self.v1})
        self.assertIn("slancha job job_1 --wait follows it.", out)
        out = self.slancha("scan", "demo", "--wait")           # queued, running, succeeded
        self.assertEqual(self.sent(f"/api/skills/demo/scan"), {})
        self.assertIn("  queued", out)
        self.assertIn(f"Job job_2  scan  demo  {self.v1[:12]}  succeeded", out)
        doc = self.as_json("scan", "demo")
        self.assertEqual((doc["id"], doc["type"], doc["status"]), ("job_3", "scan", "queued"))


class DraftAndOutcomesTests(AgentCase):
    def test_draft_needs_the_admin_opt_in_and_acknowledges_it(self):
        out = self.slancha("draft", "demo", code=1)
        self.assertIn("an admin lets agent tokens start drafts and outcome jobs", out)
        self.api.agent_jobs = True
        out = self.slancha("draft", "demo")
        self.assertEqual(self.sent("/api/draft/demo"),
                         {"acknowledge": "traces-to-model-provider-v1"})
        self.assertIn("Job job_1  draft  demo", out)
        self.assertIn("slancha job job_1 --wait follows it", out)
        self.assertIn("already served", self.slancha("draft", "demo", "--new", "--project", "/w",
                                                     code=1))
        doc = self.as_json("draft", "fresh", "--new", "--project", "/work/app",
                           "--description", "A fresh skill.")
        self.assertEqual(self.sent("/api/draft/fresh"),
                         {"acknowledge": "traces-to-model-provider-v1", "project": "/work/app",
                          "description": "A fresh skill."})
        self.assertIs(doc["create"], True)
        for args in (["draft", "fresh", "--new"], ["draft", "demo", "--project", "/w"],
                     ["draft", "demo", "--description", "x"]):
            self.slancha(*args, code=2)
        help_text = " ".join(self.slancha("draft", "--help").split())
        self.assertIn("workspace's own OpenAI key", help_text)
        self.assertIn("An admin must first let agent tokens start", help_text)

    def test_outcomes(self):
        self.slancha("outcomes", "demo", code=1)                # the admin's opt-in, again
        self.api.agent_jobs = True
        out = self.slancha("outcomes", "demo", "--days", "7")
        self.assertIn("demo: 5 sessions since 2026-09-20, 1 of them run by no known revision", out)
        self.assertIn(f"  {self.v1[:12]}  4 sessions, last 2026-10-19: errored 1 (25%), "
                      "followed_up 1 (25%), no_follow_up 2 (50%)", out)
        self.assertIn("/api/skills/demo/outcomes?days=7", [p for _, p, _ in self.api.requests])
        self.assertEqual(self.as_json("outcomes", "demo")["sessions"], 5)
        out = self.slancha("outcomes", "demo", "--start")
        self.assertIn("Job job_1  outcomes  demo", out)


if __name__ == "__main__":
    unittest.main()
