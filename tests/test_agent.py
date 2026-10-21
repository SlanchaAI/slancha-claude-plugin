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
import stat
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
        self.assertIn("dev@example.test: proposer in the workspace Example Org", out)
        self.assertIn("Token: skills:read, usage:write, manage; expires ", out)
        self.assertIn("(in 30 days)", out)
        self.assertIn("As an agent token it reads skills, reviews and jobs, checks acceptance "
                      "criteria, uploads and scans", out)
        self.assertIn("Drafts and outcome jobs: not allowed until an admin turns them on in "
                      "Workspace settings. Approving, rejecting, rolling back and setting criteria "
                      "are for people in the console.", out)
        self.api.agent_jobs = True
        self.assertIn("Drafts and outcome jobs: allowed; an admin turned them on.",
                      self.slancha("whoami"))
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
        self.assertIn("HTTP 403: a token cannot do this; sign in to the console. This needs an "
                      "agent token: run slancha auth --manage.", out)
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

    def test_criteria_of_a_skill_with_nothing_stored_lists_the_rules(self):
        self.api.criteria["brand-new"] = list(RULES)
        out = self.slancha("criteria", "brand-new")
        self.assertIn("Acceptance criteria: 2 rules, set by boss@example.test", out)
        self.assertIn('  - SKILL.md must include "## When to use"\n  - scripts/run.sh must exist',
                      out)
        self.assertEqual(self.as_json("criteria", "brand-new")["labels"],
                         ['SKILL.md must include "## When to use"', "scripts/run.sh must exist"])

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

    def test_an_upload_whose_scan_could_not_start_exits_1_and_says_not_to_upload_again(self):
        self.api.proposals.clear()
        self.api.scan_error = "the job could not start: RuntimeError"
        out = self.slancha("upload", self.package(), code=1)
        self.assertIn("Uploaded demo: revision ", out)
        self.assertIn("Gate: blocked\n  blocked: its security scan did not start", out)
        self.assertIn("Security scan: the job could not start: RuntimeError. The change is stored "
                      "and waits for review, so do not upload it again: slancha scan demo --wait "
                      "starts the scan again.", out)
        self.assertNotIn("--wait follows it", out)
        doc = self.as_json("upload", self.package(), code=1)    # refused: a change already waits
        self.assertEqual((set(doc), doc["status"]), ({"error", "status"}, 409))
        self.api.proposals.clear()
        doc = self.as_json("upload", self.package(), code=1)    # stored, but not scanned
        self.assertEqual(doc["scan_error"], "the job could not start: RuntimeError")
        self.assertIn("proposal_id", doc)
        out = self.slancha("scan", "demo", "--wait", code=1)
        self.assertIn("Slancha answered HTTP 502: the job could not start: RuntimeError. That is on "
                      "Slancha's side, not in what was sent: try again later, and tell an admin if "
                      "it keeps failing.", out)

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
        # The gate alone would allow it; the scan that is not clean yet still blocks approval.
        self.assertIn("Gate: blocked\n  blocked: no clean security scan of this revision yet", out)
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
        self.api.jobs["job_1"]["created_at"] = 100
        self.api.jobs["job_2"]["created_at"] = 200
        out = self.slancha("jobs")
        self.assertLess(out.index("job_2"), out.index("job_1"))
        self.assertIn(f"  job_1  scan  demo  {self.v1[:12]}  queued", out)
        self.assertIn("  job_2  draft  other", out)
        self.assertEqual([j["id"] for j in self.as_json("jobs")], ["job_2", "job_1"])
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
        self.assertIn("agent tokens cannot start drafts or outcome jobs until an admin allows it: "
                      "\"Let agent tokens start drafts and outcome jobs\" in Workspace settings", out)
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


def _skill_files(name: str, complete: bool = True) -> dict:
    files = {"SKILL.md": f"---\nname: {name}\ndescription: A demo skill.\n---\n"
                         "## When to use\nAlways, for the demo.\n"}
    if complete:
        files["scripts/run.sh"] = "echo hi\n"
    return files


class BulkUploadTests(AgentCase):
    """Several skills in one `slancha upload`: each is its own proposal, and the run continues."""

    def write_skill(self, root: Path, name: str, complete: bool = True) -> Path:
        dest = root / name
        for path, text in _skill_files(name, complete).items():
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self.api.criteria[name] = list(RULES)
        return dest

    def zip_skill(self, dest: Path, name: str, complete: bool = True) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w") as bundle:
            for path, text in _skill_files(name, complete).items():
                bundle.writestr(f"{name}/{path}", text)
        self.api.criteria[name] = list(RULES)
        return dest

    def test_a_folder_of_skills_uploads_each_and_skips_the_rest(self):
        parent = self.tmp / "library"
        self.write_skill(parent, "alpha")
        self.write_skill(parent, "beta")
        self.zip_skill(parent / "gamma.zip", "gamma")
        (parent / "README.md").write_text("not a skill\n")
        (parent / "notes").mkdir()
        out = self.slancha("upload", str(parent))
        self.assertEqual(set(self.api.proposals), {"alpha", "beta", "gamma"})
        self.assertIn("Skipped in", out)
        self.assertIn("README.md", out)
        self.assertIn("notes/", out)
        self.assertIn("3 skills: 3 ok, 0 failed acceptance criteria, 0 failed.", out)
        self.assertEqual([path for path, _ in self.api.bodies if path == "/api/skills/upload"],
                         ["/api/skills/upload"] * 3)
        # a directory that itself has SKILL.md is one skill, nested folders included
        root = self.write_skill(self.tmp / "src", "parent-skill")
        (root / "child").mkdir()
        (root / "child" / "SKILL.md").write_text(_skill_files("child-skill")["SKILL.md"])
        self.slancha("upload", str(root))
        self.assertEqual(set(self.api.proposals) - {"alpha", "beta", "gamma"}, {"parent-skill"})
        self.assertIn("child/SKILL.md", self.api.proposals["parent-skill"]["files"])

    def test_mixed_results_keep_going_and_criteria_set_the_exit_code(self):
        good = self.write_skill(self.tmp / "src", "good-one")
        bad = self.write_skill(self.tmp / "src", "bad-one", complete=False)
        out = self.slancha("upload", str(good), str(bad), "/no/such/skill", code=3)
        self.assertIn("Uploaded good-one", out)
        self.assertIn("Uploaded bad-one", out)
        self.assertIn("fails 1 of 2", out)
        self.assertIn("neither a directory nor a .zip", out)
        self.assertIn("3 skills: 1 ok, 1 failed acceptance criteria, 1 failed.", out)
        self.assertEqual(set(self.api.proposals), {"good-one", "bad-one"})
        other = self.write_skill(self.tmp / "src", "json-good")
        missed = self.write_skill(self.tmp / "src", "json-bad", complete=False)
        doc = self.as_json("upload", str(other), str(missed), "/also/missing", code=3)
        self.assertEqual([row["exit"] for row in doc], [0, 3, 1])
        self.assertTrue(doc[0]["uploaded"])
        self.assertIn("proposal_id", doc[0]["result"])
        self.assertTrue(doc[1]["uploaded"])
        self.assertFalse(doc[1]["result"]["criteria"]["ok"])
        self.assertFalse(doc[2]["uploaded"])
        self.assertIsNone(doc[2]["status"])

    def test_pending_is_reported_and_does_not_stop_the_others(self):
        good = self.write_skill(self.tmp / "src", "good-one")
        self.slancha("upload", str(good))
        other = self.write_skill(self.tmp / "src", "other-one")
        out = self.slancha("upload", str(good), str(other), code=1)
        self.assertIn("good-one: a change is already waiting for review, so it takes no other "
                      "upload until that change is approved or rejected. "
                      "slancha pending good-one shows it.", out)
        self.assertIn("Uploaded other-one", out)
        self.assertEqual(set(self.api.proposals), {"good-one", "other-one"})
        # two copies of the same skill in one command: the second finds the first
        first = self.write_skill(self.tmp / "src", "dup-skill")
        second = self.tmp / "src" / "dup-copy"
        second.mkdir()
        for path, text in _skill_files("dup-skill").items():
            target = second / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        out = self.slancha("upload", str(first), str(second), code=1)
        self.assertEqual(out.count("Uploaded dup-skill"), 1)
        self.assertIn("dup-skill: a change is already waiting for review", out)
        dup_uploads = [body for path, body in self.api.bodies if path == "/api/skills/upload"
                       and any(isinstance(f, dict) and "name: dup-skill" in str(f.get("content"))
                               for f in body.get("files") or [])]
        self.assertEqual(len(dup_uploads), 1)

    def test_json_array_dry_run_and_name_refused_for_many(self):
        good = self.write_skill(self.tmp / "src", "good-one")
        bad = self.write_skill(self.tmp / "src", "bad-one", complete=False)
        out = self.slancha("upload", "--dry-run", str(good), str(bad), code=3)
        self.assertIn("Dry run: nothing is uploaded.", out)
        self.assertIn(f"{good} against the acceptance criteria of good-one: meets all 2", out)
        self.assertIn("fails 1 of 2", out)
        self.assertIn("Nothing was uploaded.", out)
        self.assertEqual(self.api.proposals, {})
        self.assertFalse([path for path, _ in self.api.bodies if path == "/api/skills/upload"])
        checks = [path for path, _ in self.api.bodies if path.endswith("/criteria/check")]
        self.assertEqual(checks, ["/api/skills/good-one/criteria/check",
                                  "/api/skills/bad-one/criteria/check"])
        doc = self.as_json("upload", "--dry-run", str(good), str(bad), code=3)
        self.assertEqual([(row["skill"], row["exit"], row["dry_run"], row["uploaded"],
                           row["criteria"]["ok"]) for row in doc],
                         [("good-one", 0, True, False, True), ("bad-one", 3, True, False, False)])
        one = self.as_json("upload", str(good), "--dry-run")
        self.assertIsInstance(one, dict)
        self.assertEqual((one["dry_run"], one["skill"], one["criteria"]["ok"], one["exit"]),
                         (True, "good-one", True, 0))
        self.assertEqual(self.api.proposals, {})
        out = self.slancha("upload", "--name", "good-one", str(good), str(bad), code=2)
        self.assertIn("--name names one skill; 2 were given", out)
        self.assertEqual(self.api.proposals, {})
        err = self.as_json("upload", "--name", "good-one", str(good), str(bad), code=2)
        self.assertEqual(set(err), {"error", "status"})
        self.assertIsNone(err["status"])
        # one skill in a folder is still a batch: --json is an array, and --name applies
        parent = self.tmp / "only"
        self.write_skill(parent, "alpha")
        named = self.as_json("upload", "--name", "renamed-skill", str(parent))
        self.assertIsInstance(named, list)
        self.assertEqual(named[0]["skill"], "renamed-skill")
        self.assertEqual(set(self.api.proposals), {"renamed-skill"})
        help_text = " ".join(self.slancha("upload", "--help").split())
        self.assertIn("--dry-run", help_text)
        self.assertIn("folder of skills", help_text)
        self.assertIn("200 files", help_text)

    def test_dry_run_reports_a_pending_skill_and_a_bad_package_without_uploading(self):
        good = self.write_skill(self.tmp / "src", "good-one")
        self.slancha("upload", str(good))
        out = self.slancha("upload", "--dry-run", str(good), code=1)
        self.assertIn("already waiting for review", out)
        self.assertIn("An upload would be refused", out)
        self.assertIn("Nothing was uploaded.", out)
        self.assertEqual(set(self.api.proposals), {"good-one"})
        self.assertTrue(any(path.endswith("/criteria/check") for path, _ in self.api.bodies))
        messy = self.write_skill(self.tmp / "src", "messy")
        (messy / "logo.md").write_bytes(b"\xff\xfe")
        before = list(self.api.bodies)
        out = self.slancha("upload", "--dry-run", str(messy), code=1)
        self.assertIn("logo.md is not UTF-8 text", out)
        self.assertEqual(self.api.bodies, before)
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertIn("has no skill to upload", self.slancha("upload", str(empty), code=1))
        self.assertIn("neither a directory nor a .zip",
                      self.slancha("upload", str(self.tmp / "missing.txt"), code=1))
        (self.tmp / "notes.txt").write_text("hello\n")
        self.assertIn("not a .zip", self.slancha("upload", str(self.tmp / "notes.txt"), code=1))

    def test_each_skill_keeps_the_upload_limits(self):
        good = self.write_skill(self.tmp / "src", "good-one")
        huge = self.write_skill(self.tmp / "src", "huge-one")
        (huge / "big.md").write_text("x" * (256 * 1024 + 1))
        linked = self.write_skill(self.tmp / "src", "linked")
        (linked / "link.md").symlink_to(linked / "SKILL.md")
        slip = self.tmp / "slip.zip"
        with zipfile.ZipFile(slip, "w") as bundle:
            bundle.writestr("../SKILL.md", "x\n")
        link_zip = self.tmp / "link.zip"
        info = zipfile.ZipInfo("zip-link.md")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(link_zip, "w") as bundle:
            bundle.writestr(info, "SKILL.md")
        crowded = self.tmp / "crowded.zip"
        with zipfile.ZipFile(crowded, "w") as bundle:
            for number in range(201):
                bundle.writestr(f"f{number}.md", "x\n")
        out = self.slancha("upload", str(good), str(huge), str(linked), str(slip), str(link_zip),
                           str(crowded), code=1)
        self.assertIn("too large at big.md", out)
        self.assertIn("link.md is a link", out)
        self.assertIn("../SKILL.md' leaves the skill's folder", out)
        self.assertIn("zip-link.md is a link", out)
        self.assertIn("too large at f200.md", out)
        self.assertEqual(set(self.api.proposals), {"good-one"})
        self.assertIn("Uploaded good-one", out)
        self.assertIn("6 skills: 1 ok, 0 failed acceptance criteria, 5 failed.", out)

    def test_a_zip_is_named_by_its_folder_when_skill_md_differs(self):
        archive = self.tmp / "odd.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("alpha/SKILL.md", _skill_files("beta")["SKILL.md"])
            bundle.writestr("alpha/scripts/run.sh", "echo hi\n")
        self.api.criteria["alpha"] = list(RULES)
        doc = self.as_json("upload", "--dry-run", str(archive))
        self.assertEqual(doc["skill"], "alpha")
        self.assertTrue(doc["criteria"]["ok"])
        self.assertEqual(self.api.proposals, {})
        self.slancha("upload", str(archive))
        self.assertEqual(set(self.api.proposals), {"alpha"})
        self.assertNotIn("beta", self.api.proposals)


class DocumentedSurfaceTests(AgentCase):
    """Every command the install docs name, against the fake API or by its --help."""

    def test_flags_usage_and_a_pass_over_each_command(self):
        for args in (["--help"], ["auth", "--help"], ["init", "--help"], ["tracing", "--help"],
                     ["tracing", "status", "--help"], ["tracing", "on", "--help"],
                     ["tracing", "off", "--help"], ["tracing", "config", "--help"],
                     ["whoami", "--help"], ["skills", "--help"], ["skill", "--help"],
                     ["upload", "--help"], ["pending", "--help"], ["review", "--help"],
                     ["jobs", "--help"], ["job", "--help"], ["scan", "--help"],
                     ["draft", "--help"], ["outcomes", "--help"], ["criteria", "--help"],
                     ["update", "--help"], ["status", "--help"], ["uninstall", "--help"]):
            self.assertIn("usage:", self.slancha(*args).lower())
        auth = self.slancha("auth", "--help")
        for flag in ("--manage", "--tracing", "--no-browser", "--api"):
            self.assertIn(flag, auth)
        init = self.slancha("init", "--help")
        self.assertIn("--dry-run", init)
        self.assertIn("--tracing", init)
        update = self.slancha("update", "--help")
        self.assertIn("--dry-run", update)
        self.assertIn("--force", update)
        self.assertIn("--max-chars", self.slancha("tracing", "config", "--help"))
        self.assertIn("--images", self.slancha("tracing", "config", "--help"))
        self.assertIn("--project", self.slancha("tracing", "on", "--help"))
        self.assertIn("--json", self.slancha("whoami", "--help"))
        self.assertIn("--json", self.slancha("tracing", "status", "--help"))
        for args in (["upload"], ["skill"], ["tracing"], ["job"], ["no-such-command"],
                     ["criteria", "Demo"], ["scan", "demo", "--revision", "abc"]):
            self.slancha(*args, code=2)
        self.assertIn("Example Org", self.slancha("whoami"))
        self.assertIn("demo", self.slancha("skills"))
        self.assertIn("serves", self.slancha("skill", "demo"))
        self.assertIn("Acceptance criteria", self.slancha("criteria", "demo"))
        self.slancha("outcomes", "demo", code=1)
        self.slancha("draft", "demo", code=1)
        self.api.agent_jobs = True
        self.assertIn("sessions", self.slancha("outcomes", "demo", "--days", "30"))
        self.slancha("outcomes", "demo", "--start")
        self.slancha("draft", "demo")
        self.slancha("scan", "demo")
        self.assertIn("job_", self.slancha("jobs"))
        self.slancha("job", next(iter(self.api.jobs)))
        self.assertIn("API", self.slancha("status"))
        self.assertIn("No Slancha skills", self.slancha("update"))
        self.slancha("hook")
        self.assertIn("nothing to upload", self.slancha("flush"))
        status = self.slancha("tracing", "status")
        self.assertIn("On this machine", status)
        self.assertIn("In your workspace", status)
        doc = self.as_json("tracing", "status")
        self.assertIn("local", doc)
        self.assertIn("workspace", doc)


if __name__ == "__main__":
    unittest.main()
