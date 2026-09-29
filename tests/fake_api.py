"""A stand-in for the three Slancha API routes the plugin calls, built from the server's contract:

- GET  /api/skills                     the library: name, description, revision, active, ...
- GET  /api/skills/{skill}/download    a zip of `<skill>/<path>` + `<skill>/slancha-manifest.json`,
                                       with X-Slancha-Revision and X-Slancha-Sha256 headers
- POST /api/usage/skills               {events: [...]}, 1-500 per call, idempotent by id

Standard library only; runs in a thread on 127.0.0.1.
"""
from __future__ import annotations

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
        self.zip_for = None                  # callable(skill, revision) -> (bytes, headers)
        self.usage_status: int | None = None     # answer usage posts with this status instead
        self.store_then_fail = False         # store the events, then answer 503 (a lost reply)
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
            fake.requests.append((self.command, self.path, self.headers.get("Authorization")))
            if fake.redirect:
                self.send(302, headers={"Location": fake.redirect + self.path})
                return False
            if self.headers.get("Authorization") != f"Bearer {fake.token}":
                self.send_json(401, {"detail": "unknown personal token"})
                return False
            return True

        def do_GET(self):
            if not self.admitted():
                return
            url = urlsplit(self.path)
            if url.path == "/api/skills":
                return self.send_json(200, fake.library())
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

        def do_POST(self):
            if not self.admitted():
                return
            if urlsplit(self.path).path != "/api/usage/skills":
                return self.send_json(404, {"detail": "not found"})
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
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
