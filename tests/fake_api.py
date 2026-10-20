"""A stand-in for the Slancha API routes the plugin calls, built from the server's contract:

- GET  /api/skills                     the library: name, description, revision, active, ...
- GET  /api/skills/{skill}/download    a zip of `<skill>/<path>` + `<skill>/slancha-manifest.json`,
                                       with X-Slancha-Revision and X-Slancha-Sha256 headers
- POST /api/usage/skills               {events: [...]}, 1-500 per call, idempotent by id
- POST /api/cli/device, /api/cli/token the device authorization grant (no token)

- GET  /api/me                         role, capabilities and expires_at (an agent token's)

and, for an agent token only (the plain token gets 403, as from the server), FakeSlancha.answer:

- GET  /api/tracing/status             the workspace's Langfuse: configured, traces, per skill
- GET  /api/skills/{s}/versions, /api/skills/{s}/criteria, /api/pending, /api/pending/{s},
       /api/pending/{s}/comparison (paged), /api/evidence/{s}, /api/jobs, /api/jobs/{id},
       /api/skills/{s}/outcomes
- POST /api/skills/upload, /api/skills/{s}/criteria/check, /api/skills/{s}/scan,
       /api/jobs/{id}/cancel, /api/draft/{s}, /api/skills/{s}/outcomes

Standard library only; runs in a thread on 127.0.0.1.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import io
import json
import re
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN = "slancha_pat_test-token-1"       # short on purpose: not shaped like a real token
DEVICE_TOKEN = "slancha_pat_device-token-2"
AGENT_TOKEN = "slancha_pat_agent-token-3"  # capability "manage" (slancha auth --manage)
PLAIN_ONLY = "a token cannot do this; sign in to the console"
# What https://api.slancha.ai answered every request with on 2026-09-30: an AWS throttle, no detail.
NOT_SLANCHA = {"Reason": "ReservedFunctionConcurrentInvocationLimitExceeded", "Type": "User",
               "message": "Rate Exceeded."}
TRACING = {"host": "https://tracing.example.test", "public_key": "pk-test-public",
           "secret_key": "sk-test-secret-value"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def entry(name: str, mode: int = 0o644) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.external_attr = mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def skill_zip(skill: str, files: dict[str, bytes], revision: str, *, manifest: dict | None = None,
              extra: list = ()) -> bytes:
    """The zip the server's library.download() builds, optionally with a different manifest or
    extra (ZipInfo | name, bytes) entries appended."""
    doc = manifest if manifest is not None else {
        "schema_version": "slancha/skill-download/v1", "skill": skill, "revision": revision,
        "published_at": 1790000000.0, "active": True,
        "files": [{"path": p, "sha256": sha256(d), "size": len(d)}
                  for p, d in sorted(files.items())]}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        entries = [(f"{skill}/{p}", d) for p, d in sorted(files.items())]
        entries.append((f"{skill}/slancha-manifest.json",
                        (json.dumps(doc, sort_keys=True, indent=2) + "\n").encode()))
        for name, data in entries + list(extra):
            bundle.writestr(name if isinstance(name, zipfile.ZipInfo) else entry(name), data)
    return buffer.getvalue()


_EVENT_ID = re.compile(r"[0-9a-fA-F-]{8,64}")
_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


class FakeSlancha:
    def __init__(self, token: str = TOKEN):
        self.token = token
        self.published: dict[str, tuple[str, dict[str, bytes]]] = {}
        self.rows: list[dict] = []           # extra library rows, e.g. a skill pending review
        self.usage: dict[str, dict] = {}     # stored events by id
        self.requests: list[tuple[str, str, str | None]] = []
        self.redirect: str | None = None     # answer everything with a 302 to this origin
        self.expired = False                 # answer every token with 401 "this token expired"
        self.scan_error = None               # the upload's scan could not start: this text
        self.not_slancha = False             # answer everything as a throttled AWS front would
        self.zip_for = None                  # callable(skill, revision) -> (bytes, headers)
        self.usage_status: int | None = None     # answer usage posts with this status instead
        self.store_then_fail = False         # store the events, then answer 503 (a lost reply)
        self.device_bodies: list[dict] = []  # what /api/cli/device and /api/cli/token received
        self.pending = 1                     # polls answered authorization_pending before approval
        self.outcome = "approve"             # or "expired", or "pending" (never approved)
        self.expires_in = 600
        self.polls = 0
        self.tracing: dict | None = dict(TRACING)
        self.tracing_status: dict = {"configured": False, "reachable": False, "connected": False,
                                     "requested_at": None, "checked_at": 1790000000.0,
                                     "cached": False}
        self.bodies: list[tuple[str, dict]] = []   # what each agent-route POST received
        self.criteria: dict[str, list[dict]] = {}  # rules per skill: includes and file only
        self.proposals: dict[str, dict] = {}       # the change waiting for review, per skill
        self.jobs: dict[str, dict] = {}
        self.job_end = "succeeded"                 # where a job goes on its second read
        self.agent_jobs = False                    # the admin's opt-in for drafts and outcomes
        self.page = 40                             # characters per comparison page
        self.refuse: dict[str, tuple[int, str]] = {}   # path -> (status, detail), for any method
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def publish(self, skill: str, files: dict) -> str:
        files = {p: d.encode() if isinstance(d, str) else d for p, d in files.items()}
        revision = sha256(json.dumps({p: sha256(d) for p, d in sorted(files.items())}).encode())
        self.published[skill] = (revision, files)
        return revision

    def library(self) -> list[dict]:
        rows = [{"name": name, "description": f"{name} skill", "revision": revision,
                 "active": True, "updated_at": 1790000000.0, "pending": False,
                 "pending_kind": None, "uses_7d": 0}
                for name, (revision, _) in sorted(self.published.items())]
        return rows + self.rows

    def record_usage(self, events) -> int:
        """pgstore.record_usage's checks: all or nothing, idempotent by id."""
        if not isinstance(events, list) or not 1 <= len(events) <= 500:
            raise ValueError("send between 1 and 500 events at a time")
        now, rows = time.time(), []
        for event in events:
            revision = event.get("revision") or "unknown"
            at = event.get("at")
            if not (isinstance(event.get("id"), str) and _EVENT_ID.fullmatch(event["id"])):
                raise ValueError("each event needs an id (a uuid)")
            if not (isinstance(event.get("skill"), str) and _SKILL_NAME.fullmatch(event["skill"])):
                raise ValueError("not a skill name")
            if not (revision == "unknown" or re.fullmatch(r"[0-9a-f]{12,64}", revision)):
                raise ValueError("a revision is a hex digest or 'unknown'")
            if event.get("kind") not in ("invoked", "reference_read"):
                raise ValueError("kind is one of invoked, reference_read")
            if event.get("success") not in (True, False, None):
                raise ValueError("success is true, false or null")
            if not (isinstance(event.get("session"), str) and _HEX64.fullmatch(event["session"])):
                raise ValueError("session is the sha256 of the session id, not the id")
            if not (isinstance(at, (int, float)) and now - 30 * 86400 <= at <= now + 300):
                raise ValueError("at is a unix time within the last 30 days")
            rows.append(event)
        new = 0
        for event in rows:
            if event["id"].lower() not in self.usage:
                self.usage[event["id"].lower()] = event
                new += 1
        return new

    # --- what an agent token reaches (CONTRACT.md) ---------------------------------------------

    def me(self, agent: bool) -> dict:
        return {"user": "user-1", "email": "dev@example.test", "machine": True,
                "role": "proposer" if agent else "viewer",
                "capabilities": ["skills:read", "usage:write"] + (["manage"] if agent else []),
                "expires_at": time.time() + 30 * 86400 if agent else None,
                "org": {"id": "org-1", "name": "Example Org", "status": "active"},
                "features": {"mining": True, "tracing": True}, "orgs": []}

    def evaluate(self, skill: str, files: dict[str, str]) -> dict:
        results = []
        for rule in self.criteria.get(skill, []):
            path, content = rule["path"], files.get(rule["path"])
            if rule["type"] == "includes":
                ok = content is not None and rule["text"] in content
                reason = f"{path} is missing" if content is None else \
                    f'{path} does not include "{rule["text"]}"'
            else:
                ok, reason = content is not None, f"{path} is missing"
            results.append({"rule": rule, "label": self.label(rule), "ok": ok,
                            "reason": None if ok else reason})
        return {"ok": all(r["ok"] for r in results), "results": results,
                "failures": [f"{r['label']}: {r['reason']}" for r in results if not r["ok"]]}

    @staticmethod
    def label(rule: dict) -> str:
        if rule["type"] == "includes":
            return f'{rule["path"]} must include "{rule["text"]}"'
        return f"{rule['path']} must exist"

    @staticmethod
    def unpack(body) -> tuple[str | None, dict[str, str]]:
        """upload.parse in short: (the name it implies, {path: text}) of a package body."""
        if isinstance(body, dict) and isinstance(body.get("archive"), str):
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(body["archive"]))) as bundle:
                names = [n for n in bundle.namelist() if not n.endswith("/")]
                tops = {n.split("/", 1)[0] for n in names}
                folder = next(iter(tops)) if len(tops) == 1 and all("/" in n for n in names) \
                    else None
                return folder, {(n.split("/", 1)[1] if folder else n): bundle.read(n).decode()
                                for n in names}
        if isinstance(body, dict) and isinstance(body.get("files"), list) and body["files"]:
            return None, {f["path"]: f["content"] for f in body["files"]}
        raise ValueError("send the skill as files or as a zip archive")

    def start_job(self, type: str, skill: str, revision: str, **request) -> dict:
        job = {"id": f"job_{len(self.jobs) + 1}", "type": type, "skill": skill,
               "revision": revision, "status": "queued", "progress": None, "result": None,
               "error": None, "cost_usd": 0, "created_by": "dev@example.test",
               "created_at": time.time(), "limits": {}, "create": bool(request.get("create")),
               "request": request}
        self.jobs[job["id"]] = job
        return job

    def card(self, skill: str) -> dict:
        record = self.proposals[skill]
        evaluation = self.evaluate(skill, record["files"])
        gate = {**record["gate"], "promotable": record["gate"]["promotable"] and evaluation["ok"],
                "blocked": record["gate"]["blocked"] + [f"acceptance criteria: {f}"
                                                        for f in evaluation["failures"]]}
        changed = sorted(p for p in set(record["files"]) | set(record["base_files"])
                         if record["files"].get(p) != record["base_files"].get(p))
        return {**{k: v for k, v in record.items() if k not in ("files", "base_files")},
                "gate": gate, "criteria": evaluation, "stale": None, "risk": None, "scan": None,
                "scan_blocker": "no clean security scan of this revision yet",
                "changed": changed, "comparison_deferred": True, "comparison": [], "diff": "",
                "comparison_components": [{"id": f"file:{p}", "label": p} for p in changed],
                "files": [{"path": p, "status": "added" if p not in record["base_files"]
                           else "modified"} for p in changed]}

    def answer(self, method: str, path: str, query: dict, body) -> tuple[int, object]:
        """(status, JSON) for the routes past the plain token's, as the contract describes them."""
        if path in self.refuse:
            status, detail = self.refuse[path]
            return status, {"detail": detail}
        found = re.fullmatch(r"/api/(?:skills|pending|evidence|draft)/([a-z0-9-]+)(/.*)?", path)
        skill, rest = (found.group(1), found.group(2) or "") if found else (None, "")
        record = self.proposals.get(skill)
        served = self.published.get(skill, (None, {}))
        agent_only = ("agent tokens cannot start drafts or outcome jobs until an admin allows it: "
                      "\"Let agent tokens start drafts and outcome jobs\" in Workspace settings in "
                      "the console")
        if (method, path) == ("GET", "/api/tracing/status"):
            return 200, self.tracing_status
        if (method, path) == ("GET", "/api/pending"):
            return 200, [{k: r[k] for k in ("skill", "id", "kind", "revision", "base_revision",
                                            "created_by")} | {"created_at": r["created"]}
                         for r in self.proposals.values()]
        if (method, path) == ("POST", "/api/skills/upload"):
            try:
                top, files = self.unpack(body)
            except ValueError as exc:
                return 400, {"detail": str(exc)}
            name = body.get("name") or top \
                or re.search(r"name:\s*(\S+)", files.get("SKILL.md", ""))[1]
            revision = sha256(json.dumps(files, sort_keys=True).encode())
            if name in self.proposals:
                return 409, {"detail": f"a change to '{name}' is already waiting for review; "
                                       "approve or reject it first"}
            base_revision, base_files = self.published.get(name, ("absent", {}))
            self.proposals[name] = {
                "id": f"proposal-{len(self.bodies)}", "skill": name, "source": "upload",
                "kind": "update" if name in self.published else "creation",
                "revision": revision, "base_revision": base_revision, "created": time.time(),
                "created_by": "dev@example.test", "files": files,
                "base_files": {p: d.decode() for p, d in base_files.items()},
                "gate": {"promotable": True, "blocked": [], "warnings": []},
                "evidence": {"schema_version": "slancha/upload/v1"}}
            scan = self.start_job("scan", name, revision)
            return 201, {"skill": name, "proposal_id": self.proposals[name]["id"],
                         "kind": self.proposals[name]["kind"], "revision": revision,
                         "gate": self.card(name)["gate"],
                         "scan_job": None if self.scan_error else scan["id"],
                         "scan_error": self.scan_error, "criteria": self.evaluate(name, files)}
        if path == "/api/jobs":
            return 200, [j for j in self.jobs.values() if query.get("skill") in (None, j["skill"])]
        found = re.fullmatch(r"/api/jobs/([^/]+)(/cancel)?", path)
        if found:
            job = self.jobs.get(found.group(1))
            if job is None:
                return 404, {"detail": "no such job"}
            if found.group(2):
                job["status"] = "cancelled"
            elif job["status"] not in ("succeeded", "failed", "cancelled"):
                job["status"] = "running" if job["status"] == "queued" else self.job_end
                job["result"] = {"ok": True} if job["status"] == "succeeded" else None
            return 200, job
        if skill is None:
            return 404, {"detail": "not found"}
        if (method, rest) == ("GET", "/versions"):
            if served[0] is None and record is None:
                return 404, {"detail": f"no skill named '{skill}'"}
            return 200, {"skill": skill, "active": served[0],
                         "pending": record and {k: record[k] for k in ("id", "revision", "kind",
                                                                       "created")},
                         "publications": [{"id": "pub-1", "action": "approve",
                                           "from_revision": "absent", "to_revision": served[0],
                                           "proposal_id": "proposal-0",
                                           "actor": "boss@example.test",
                                           "at": 1790000000.0}] if served[0] else []}
        if (method, rest) == ("GET", "/criteria"):
            rules = self.criteria.get(skill, [])
            return 200, {"skill": skill, "rules": rules, "labels": [self.label(r) for r in rules],
                         "updated_by": "boss@example.test" if rules else None,
                         "updated_at": 1790000000.0 if rules else None,
                         "served": served[0] and {"revision": served[0], **self.evaluate(
                             skill, {p: d.decode() for p, d in served[1].items()})},
                         "pending": record and {"revision": record["revision"],
                                                "proposal_id": record["id"],
                                                **self.evaluate(skill, record["files"])}}
        if (method, rest) == ("POST", "/criteria/check"):
            if isinstance(body, dict) and "revision" in body:
                stored = {served[0]: {p: d.decode() for p, d in served[1].items()},
                          **({record["revision"]: record["files"]} if record else {})}
                if body["revision"] not in stored:
                    return 404, {"detail": f"'{skill}' has no revision {body['revision'][:12]}"}
                return 200, {"skill": skill, "revision": body["revision"],
                             **self.evaluate(skill, stored[body["revision"]])}
            try:
                return 200, {"skill": skill, "revision": None,
                             **self.evaluate(skill, self.unpack(body)[1])}
            except ValueError as exc:
                return 400, {"detail": str(exc)}
        if path.startswith("/api/pending/") and record is None:
            return 404, {"detail": f"no pending change for '{skill}'"}
        if (method, path) == ("GET", f"/api/pending/{skill}"):
            if query.get("comparison_paging") != "true":
                return 409, {"detail": "This review requires the updated comparison viewer; "
                                       "reload the console before reviewing it."}
            return 200, self.card(skill)
        if (method, path) == ("GET", f"/api/pending/{skill}/comparison"):
            if query.get("proposal_id") != record["id"]:
                return 409, {"detail": "this change was replaced after you opened it"}
            component = query["component"].removeprefix("file:")
            content = "\n".join(difflib.unified_diff(
                record["base_files"].get(component, "").splitlines(),
                record["files"].get(component, "").splitlines(),
                fromfile=f"{component} (base)", tofile=f"{component} (proposed)", lineterm=""))
            offset = int(query.get("offset", 0))
            end = min(offset + self.page, len(content))
            return 200, {"proposal_id": record["id"], "component": query["component"],
                         "view": query.get("view"), "offset": offset,
                         "content": content[offset:end],
                         "next_offset": end if end < len(content) else None,
                         "total_chars": len(content)}
        if (method, path) == ("GET", f"/api/evidence/{skill}"):
            if record is None:
                return 404, {"detail": f"no pending change for '{skill}'"}
            return 200, {"skill": skill, "proposal_id": record["id"],
                         "gate": self.card(skill)["gate"], "evidence": record["evidence"]}
        if (method, rest) == ("POST", "/scan"):
            revision = (body or {}).get("revision") or (record or {}).get("revision") or served[0]
            if revision is None:
                return 404, {"detail": f"nothing of '{skill}' is stored to scan"}
            if self.scan_error:                  # as job_service.StartFailed answers
                return 502, {"detail": self.scan_error}
            return 202, self.start_job("scan", skill, revision)
        if path in (f"/api/draft/{skill}", f"/api/skills/{skill}/outcomes") and not self.agent_jobs:
            return 403, {"detail": agent_only}
        if (method, path) == ("POST", f"/api/draft/{skill}"):
            if body.get("acknowledge") != "traces-to-model-provider-v1":
                return 400, {"detail": "an org admin acknowledges that with acknowledge: "
                                       "\"traces-to-model-provider-v1\""}
            if served[0] is None and not body.get("project"):
                return 400, {"detail": "a new skill is written from the sessions of one project"}
            return 202, self.start_job("draft", skill, served[0] or "0" * 64,
                                       **({"create": True, "project": body["project"]}
                                          if served[0] is None else {}))
        if (method, rest) == ("POST", "/outcomes"):
            return 202, self.start_job("outcomes", skill, served[0])
        if (method, rest) == ("GET", "/outcomes"):
            return 200, {"skill": skill, "days": int(query.get("days", 30)), "since": "2026-09-20",
                         "sessions": 5, "unattributed": 1, "attribution": "a session counts "
                         "under the revision its usage event names",
                         "revisions": [{"revision": served[0], "sessions": 4,
                                        "last_day": "2026-10-19",
                                        "counts": {"errored": 1, "followed_up": 1,
                                                   "no_follow_up": 2},
                                        "rates": {"errored": 0.25, "followed_up": 0.25,
                                                  "no_follow_up": 0.5}}]}
        return 404, {"detail": "not found"}


def _handler(fake: FakeSlancha):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status: int, body: bytes = b"", headers: dict | None = None,
                 content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, status: int, doc) -> None:
            self.send(status, json.dumps(doc).encode())

        def admitted(self) -> bool:
            """The plain token for the plugin's own routes; the agent token for every route."""
            fake.requests.append((self.command, self.path, self.headers.get("Authorization")))
            path = urlsplit(self.path).path
            plain = path in ("/api/skills", "/api/me", "/api/usage/skills") \
                or re.fullmatch(r"/api/skills/[^/]+/download", path)
            if fake.not_slancha:
                self.send_json(429, NOT_SLANCHA)
                return False
            if fake.redirect:
                self.send(302, headers={"Location": fake.redirect + self.path})
                return False
            if fake.expired:
                self.send_json(401, {"detail": "this token expired; run slancha auth again"})
                return False
            auth = self.headers.get("Authorization")
            if auth == f"Bearer {AGENT_TOKEN}" or (plain and auth == f"Bearer {fake.token}"):
                return True
            if auth == f"Bearer {fake.token}":
                self.send_json(403, {"detail": PLAIN_ONLY})
            else:
                self.send_json(401, {"detail": "unknown personal token"})
            return False

        def do_GET(self):
            if not self.admitted():
                return
            url = urlsplit(self.path)
            if url.path == "/api/skills":
                return self.send_json(200, fake.library())
            if url.path == "/api/me":
                return self.send_json(200, fake.me(self.headers.get("Authorization")
                                                   == f"Bearer {AGENT_TOKEN}"))
            if not re.fullmatch(r"/api/skills/[^/]+/download", url.path):
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                return self.send_json(*fake.answer("GET", url.path, query, None))
            found = re.fullmatch(r"/api/skills/([^/]+)/download", url.path)
            if not found or found.group(1) not in fake.published:
                return self.send_json(404, {"detail": "not served"})
            skill = found.group(1)
            revision, files = fake.published[skill]
            asked = parse_qs(url.query).get("revision", [revision])[0]
            if asked != revision:
                return self.send_json(404, {"detail": f"'{skill}' never served {asked[:12]}"})
            if fake.zip_for is not None:
                data, headers = fake.zip_for(skill, revision)
            else:
                data = skill_zip(skill, files, revision)
                headers = {"X-Slancha-Revision": revision, "X-Slancha-Sha256": sha256(data)}
            self.send(200, data, headers, "application/zip")

        def device_flow(self, path: str):
            """The device authorization grant; neither route takes a token."""
            fake.requests.append((self.command, self.path, self.headers.get("Authorization")))
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            fake.device_bodies.append(body)
            if fake.not_slancha:
                return self.send_json(429, NOT_SLANCHA)
            if path == "/api/cli/device":
                return self.send_json(200, {
                    "device_code": "device-code-1", "user_code": "WDJB-MJHT",
                    "verification_uri": f"{fake.url}/cli",
                    "verification_uri_complete": f"{fake.url}/cli?code=WDJB-MJHT",
                    "expires_in": fake.expires_in, "interval": 1})
            if body.get("device_code") != "device-code-1":
                return self.send_json(400, {"error": "invalid_grant"})
            fake.polls += 1
            if fake.outcome == "expired":
                return self.send_json(400, {"error": "expired_token"})
            if fake.outcome == "pending" or fake.polls <= fake.pending:
                return self.send_json(400, {"error": "authorization_pending"})
            asked = fake.device_bodies[0]           # what /api/cli/device was sent
            manage = asked.get("manage") is True
            self.send(200, json.dumps({
                "token": DEVICE_TOKEN, "org_id": "org-1", "api": fake.url,
                "tracing": fake.tracing if asked.get("tracing") else None,
                "capabilities": ["skills:read", "usage:write"] + (["manage"] if manage else []),
                "expires_at": time.time() + 30 * 86400 if manage else None}).encode(),
                {"Cache-Control": "no-store"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path in ("/api/cli/device", "/api/cli/token"):
                return self.device_flow(path)
            if not self.admitted():
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = json.loads(raw) if raw else None        # cancel and outcomes send none
            if path != "/api/usage/skills":
                fake.bodies.append((path, payload))
                return self.send_json(*fake.answer("POST", path, {}, payload))
            if fake.usage_status:
                return self.send_json(fake.usage_status, {"detail": "refused by the test"})
            events = payload.get("events") if isinstance(payload, dict) else None
            try:
                stored = fake.record_usage(events)
            except ValueError as exc:
                return self.send_json(400, {"detail": str(exc)})
            if fake.store_then_fail:
                return self.send_json(503, {"detail": "stored, but the reply was lost"})
            self.send_json(200, {"received": len(events), "stored": stored})

    return Handler
