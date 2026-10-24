#!/usr/bin/env python3
"""Slancha for Claude Code: install and update the skills your Slancha library publishes, and
report when they are used.

Standard library only, Python 3.9 or newer. The plugin's commands and hooks run it:

    slancha.py update [skill] [--dry-run] [--force] [--project DIR]
    slancha.py status [--project DIR]
    slancha.py hook     a Claude Code hook; the event arrives as JSON on stdin
    slancha.py flush    uploads the usage spool; the hooks start it in the background

From a terminal, to set up a machine (install.sh runs both, without --tracing):

    slancha.py auth [--api URL] [--no-browser] [--tracing]   sign in; you approve the code
    slancha.py init [--dry-run] [--tracing]   configure Claude Code: the plugin, and tracing
    slancha.py tracing status|on|off|config   this machine's tracing to the org's Langfuse

and, with an agent token (slancha auth --manage), the library for a person or a coding agent:
whoami, skills, skill, upload, pending, review, jobs, job, scan, draft, outcomes and criteria,
each with --json. `upload` takes one skill, several, or a folder of them, and `--dry-run` checks
without uploading. Exit codes: 0 done, 1 an error or refusal, 2 usage, 3 acceptance criteria not
met. Approving, rejecting, rolling back and setting criteria stay with people in the console.

The token is a personal token from the Slancha console, read from SLANCHA_TOKEN or from
~/.config/slancha/token. It goes only to the configured API origin, only over HTTPS, and is never
printed. A redirect is refused rather than followed, because urllib would send the token along.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:          # Windows: no locking; appends to the spool stay whole lines anyway
    fcntl = None

VERSION = "0.2.0"
# The Slancha console's function URL. https://api.slancha.ai is the legacy router-era Lambda and
# answers every request with HTTP 429, so it is not a fallback. api_base() uses SLANCHA_API_URL,
# then {"api_url"} in ~/.config/slancha/config.json, then this.
DEFAULT_API_URL = "https://mlzdj5zapni6reahewxo3q4a4q0jxjup.lambda-url.us-east-1.on.aws"
MANIFEST = "slancha-manifest.json"
SCHEMA = "slancha/skill-download/v1"
BACKUP_DIR = ".slancha-backup"

# The server's upload bounds. A published skill never exceeds them, so a download that does is
# refused rather than unpacked.
MAX_FILES = 200
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_PATH = 200
MAX_NAME = 64
MAX_ZIP_BYTES = 3 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024

SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REVISION = re.compile(r"[0-9a-f]{64}")
TOKEN = re.compile(r"slancha_pat_[A-Za-z0-9_-]{8,256}")

# What POST /api/usage/skills accepts. It refuses a whole batch over one bad event, so every event
# is checked here first. Events are dropped half a day before the server's 30-day window closes,
# so nothing sent can age out in flight.
USAGE_KINDS = ("invoked", "reference_read")
_EVENT_ID = re.compile(r"[0-9a-fA-F-]{8,64}")
_USAGE_SKILL = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_USAGE_REVISION = re.compile(r"[0-9a-f]{12,64}")
MAX_EVENT_AGE_S = 29.5 * 86400
MAX_SPOOL_EVENTS = 5000
BATCH = 500
_EVENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://slancha.ai/claude-plugin/usage")

SETUP = ("Create a personal token in the Slancha console (Claude Code page), then store it in "
         "your own terminal, not in this chat:\n"
         "  umask 077; mkdir -p ~/.config/slancha; printf '%s' '<token>' > ~/.config/slancha/token")


class SlanchaError(Exception):
    """A problem to report to the user; the message is safe to print."""


class UsageError(SlanchaError):
    """The command line is wrong. Exit 2, and do not start the work."""


class Offline(SlanchaError):
    """The API could not be reached."""


class ApiError(SlanchaError):
    def __init__(self, status: int, message: str, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail        # the API's own `detail`, for callers that act on it


class Expired(ApiError):
    """The API refused the token because it expired: an agent token lapses after 30 days."""


class Refused(SlanchaError):
    """A download failed a check and was not installed."""


# --- configuration -------------------------------------------------------------------------------

def config_dir() -> Path:
    return Path.home() / ".config" / "slancha"


def cache_dir() -> Path:
    return Path.home() / ".cache" / "slancha"


def claude_config_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base).expanduser() if base else Path.home() / ".claude"


def user_skills_root() -> Path:
    return claude_config_dir() / "skills"


def skill_roots(project: str | None) -> list[Path]:
    """Where Claude Code reads skills, in its own precedence order: personal over project."""
    roots = [user_skills_root()]
    if project:
        roots.append(Path(project).expanduser() / ".claude" / "skills")
    seen, out = set(), []
    for root in roots:
        key = os.path.realpath(root)
        if key not in seen:
            seen.add(key)
            out.append(root)
    return out


def project_dir(explicit: str | None = None, event: dict | None = None) -> str:
    cwd = event.get("cwd") if isinstance(event, dict) else None
    return (explicit or os.environ.get("CLAUDE_PROJECT_DIR")
            or (cwd if isinstance(cwd, str) else None) or os.getcwd())


@dataclass
class Config:
    api_url: str | None
    token: str | None
    token_source: str
    problem: str | None          # why the plugin cannot talk to the API, when it cannot


def load_config() -> Config:
    token, source, problem = None, "", None
    raw = os.environ.get("SLANCHA_TOKEN")
    if raw:
        source = "SLANCHA_TOKEN"
    else:
        path = config_dir() / "token"
        source = "~/.config/slancha/token"
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")[:4096]
        except FileNotFoundError:
            problem = "No Slancha token yet. " + SETUP
        except OSError as exc:
            problem = f"Cannot read ~/.config/slancha/token ({exc.strerror})."
    if raw:
        raw = raw.strip()
        if TOKEN.fullmatch(raw):
            token = raw
        else:
            problem = (f"The token in {source} is not a Slancha personal token (it starts with "
                       "slancha_pat_). " + SETUP)
    try:
        api_url = api_base()
    except SlanchaError as exc:
        api_url, problem = None, str(exc)
    return Config(api_url, token, source, problem)


def api_base() -> str:
    raw = os.environ.get("SLANCHA_API_URL")
    where = "SLANCHA_API_URL"
    if not raw:
        where = "~/.config/slancha/config.json"
        try:
            doc = json.loads((config_dir() / "config.json").read_text(encoding="utf-8"))
            raw = doc.get("api_url") if isinstance(doc, dict) else None
        except FileNotFoundError:
            raw = None
        except (OSError, ValueError):
            raise SlanchaError(f"{where} is not valid JSON.") from None
    if not raw:
        return DEFAULT_API_URL
    if not isinstance(raw, str):
        raise SlanchaError(f"api_url in {where} is not a string.")
    return check_url(raw, where)


def check_url(raw: str, where: str) -> str:
    """An https:// URL (http:// only for 127.0.0.1 or localhost), without a trailing slash."""
    url = raw.strip()
    parts = urllib.parse.urlsplit(url)
    try:
        parts.port
    except ValueError:
        raise SlanchaError(f"{where} has an invalid port.") from None
    local = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")
    if not ((parts.scheme == "https" and parts.hostname) or local) or parts.username \
            or parts.password or parts.query or parts.fragment:
        raise SlanchaError(f"{where} must be an https:// URL (http:// only for 127.0.0.1 or "
                           "localhost), without credentials, query or fragment.")
    return url.rstrip("/")


# --- HTTP ----------------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib re-sends the Authorization header to wherever a redirect points; the token must
    reach only the configured origin, so a redirect is an error instead."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _detail(error: urllib.error.HTTPError) -> str:
    """The API's short `detail` message, never the raw body."""
    try:
        doc = json.loads(error.read(8192))
    except Exception:
        return ""
    detail = doc.get("detail") if isinstance(doc, dict) else None
    if not isinstance(detail, str):
        return ""
    # Long enough for an upload's refusal or the acceptance criteria a change fails.
    return "".join(c if c.isprintable() else " " for c in detail)[:1000]


# What an API that is not this console's looks like from here: a 404 or 429 with no Slancha
# `detail`. The legacy host https://api.slancha.ai answers every request that way.
WRONG_API = (" Is {host} your Slancha API? The Claude Code page of your Slancha console shows the "
             "commands with it: set SLANCHA_API_URL, or run slancha auth --api <url>.")
EXPIRED_DETAIL = "this token expired; run slancha auth again"
TOKEN_EXPIRED = ("Your Slancha token expired; run slancha auth in a terminal (with --manage for a "
                 "new agent token).")
# What the API answers a plain personal token on a route only people and agent tokens may use.
PLAIN_TOKEN_DETAIL = "a token cannot do this"


def request(cfg: Config, method: str, path: str, *, body=None, timeout: float = 20,
            limit: int = MAX_JSON_BYTES, accept: str = "application/json"):
    """(headers, bytes) of a successful answer; raises Offline, ApiError or SlanchaError."""
    headers = {"Authorization": f"Bearer {cfg.token}", "Accept": accept,
               "User-Agent": f"slancha-claude-plugin/{VERSION}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(cfg.api_url + path, data=data, method=method, headers=headers)
    host = urllib.parse.urlsplit(cfg.api_url).netloc
    try:
        with _OPENER.open(req, timeout=timeout) as response:
            payload = response.read(limit + 1)
            if len(payload) > limit:
                raise SlanchaError(f"{host} answered {path.split('?')[0]} with more than "
                                   f"{limit} bytes.")
            return response.headers, payload
    except urllib.error.HTTPError as error:
        with contextlib.closing(error):
            if 300 <= error.code < 400:
                raise ApiError(error.code, f"{host} answered with a redirect (HTTP {error.code}); "
                                           "not following it.") from None
            detail = _detail(error)
        if error.code == 401 and detail == EXPIRED_DETAIL:
            raise Expired(401, TOKEN_EXPIRED, detail) from None
        if error.code == 401:
            message = ("Slancha did not accept the token (HTTP 401). Create a new one in the "
                       "Slancha console (Claude Code page) and store it again.")
        else:
            message = f"Slancha answered HTTP {error.code}" + (f": {detail}" if detail else ".")
            if error.code in (404, 429) and not detail:
                message += WRONG_API.format(host=host)
            if error.code >= 500:
                message += ("" if message.endswith(".") else ".") + \
                    " That is on Slancha's side, not in what was sent: try again later, and tell " \
                    "an admin if it keeps failing."
            if error.code == 403 and detail.startswith(PLAIN_TOKEN_DETAIL):
                message += ("" if message.endswith(".") else ".") + \
                    " This needs an agent token: run slancha auth --manage."
        raise ApiError(error.code, message, detail) from None
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        reason = getattr(error, "reason", None) or error.__class__.__name__
        raise Offline(f"Cannot reach {host} ({reason}).") from None


def api_json(cfg: Config, method: str, path: str, *, body=None, timeout: float = 30):
    """The parsed JSON of an API answer; raises like request(), and with the token's problem."""
    if cfg.problem:
        raise SlanchaError(cfg.problem)
    _, payload = request(cfg, method, path, body=body, timeout=timeout)
    try:
        return json.loads(payload)
    except ValueError:
        raise SlanchaError(f"Slancha answered {path.split('?')[0]} with invalid JSON.") from None


def fetch_library(cfg: Config, timeout: float = 20) -> dict[str, dict]:
    _, payload = request(cfg, "GET", "/api/skills", timeout=timeout)
    try:
        rows = json.loads(payload)
    except ValueError:
        raise SlanchaError("Slancha answered /api/skills with invalid JSON.") from None
    if not isinstance(rows, list):
        raise SlanchaError("Slancha answered /api/skills with something other than a list.")
    return {row["name"]: row for row in rows if isinstance(row, dict)
            and isinstance(row.get("name"), str) and valid_name(row["name"])}


def published(row: dict | None) -> str | None:
    """The revision the library serves for a skill, or None (unknown, or only pending review)."""
    if not row or row.get("active") is not True:
        return None
    revision = row.get("revision")
    return revision if isinstance(revision, str) and REVISION.fullmatch(revision) else None


def download(cfg: Config, skill: str, revision: str) -> bytes:
    query = urllib.parse.urlencode({"revision": revision})
    headers, data = request(cfg, "GET", f"/api/skills/{skill}/download?{query}", timeout=60,
                            limit=MAX_ZIP_BYTES, accept="application/zip")
    served = headers.get("X-Slancha-Revision", "")
    digest = headers.get("X-Slancha-Sha256", "").strip().lower()
    if served != revision:
        raise Refused(f"the download is revision {short(served) or '(none)'}, not "
                      f"{short(revision)}")
    if not REVISION.fullmatch(digest) or hashlib.sha256(data).hexdigest() != digest:
        raise Refused("the download does not match its X-Slancha-Sha256 digest")
    return data


# --- packages ------------------------------------------------------------------------------------

def valid_name(name) -> bool:
    return isinstance(name, str) and len(name) <= MAX_NAME and bool(SKILL_NAME.fullmatch(name))


def short(revision: str) -> str:
    return revision[:12]


def shown(path: Path) -> str:
    home, text = str(Path.home()), str(path)
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def check_path(path) -> str:
    """A relative POSIX path within a skill, by the same rules the server applies at upload."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH:
        raise Refused(f"{path!r} is not a usable path")
    if "\\" in path or path.startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in path) \
            or unicodedata.normalize("NFC", path) != path:
        raise Refused(f"{path!r} is not a plain relative path")
    for part in path.split("/"):
        if part in ("", ".", "..") or part.startswith(".") or part.endswith((" ", ".")) \
                or ":" in part:
            raise Refused(f"{path!r} has an empty, hidden or '..' segment")
    return path


def parse_manifest(raw: bytes, skill: str) -> tuple[str, dict[str, tuple[str, int]]]:
    """(revision, {path: (sha256, size)}) from a slancha-manifest.json."""
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise Refused(f"{MANIFEST} is not valid JSON") from None
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA:
        raise Refused(f"{MANIFEST} is not a {SCHEMA} manifest")
    if doc.get("skill") != skill:
        raise Refused(f"{MANIFEST} names skill {doc.get('skill')!r}, not {skill!r}")
    revision, files = doc.get("revision"), doc.get("files")
    if not isinstance(revision, str) or not REVISION.fullmatch(revision) \
            or not isinstance(files, list):
        raise Refused(f"{MANIFEST} has no revision or file list")
    listed: dict[str, tuple[str, int]] = {}
    for entry in files:
        if not (isinstance(entry, dict) and isinstance(entry.get("sha256"), str)
                and REVISION.fullmatch(entry["sha256"]) and isinstance(entry.get("size"), int)):
            raise Refused(f"{MANIFEST} lists a file without a sha256 or size")
        path = check_path(entry.get("path"))
        if path == MANIFEST or path.casefold() in {p.casefold() for p in listed}:
            raise Refused(f"{MANIFEST} lists {path} twice")
        listed[path] = (entry["sha256"], entry["size"])
    return revision, listed


def unpack(data: bytes, skill: str, revision: str) -> tuple[dict[str, bytes], bytes]:
    """The package's files and its manifest bytes, every file checked against the manifest.
    Nothing touches the disk here."""
    try:
        bundle = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        raise Refused("the download is not a zip file") from None
    files: dict[str, bytes] = {}
    manifest, seen, total = None, set(), 0
    with bundle:
        entries = bundle.infolist()
        if len(entries) > MAX_FILES + 1:
            raise Refused(f"the zip holds {len(entries)} entries; a skill has at most {MAX_FILES} "
                          "files")
        for info in entries:
            name = info.filename
            kind = stat.S_IFMT(info.external_attr >> 16)
            if info.is_dir() or kind not in (0, stat.S_IFREG):
                raise Refused(f"{name!r} is not a regular file (links and directories are refused)")
            if info.flag_bits & 0x1:
                raise Refused(f"{name!r} is encrypted")
            if not name.startswith(skill + "/"):
                raise Refused(f"{name!r} is outside {skill}/")
            path = check_path(name[len(skill) + 1:])
            if path.casefold() in seen:
                raise Refused(f"{path} appears twice in the zip")
            seen.add(path.casefold())
            limit = MAX_MANIFEST_BYTES if path == MANIFEST else MAX_FILE_BYTES
            if info.file_size > limit:
                raise Refused(f"{path} is {info.file_size} bytes; the limit is {limit}")
            try:
                with bundle.open(info) as member:
                    content = member.read(limit + 1)     # the size header is not trusted
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError,
                    zlib.error):
                raise Refused(f"{path} could not be read from the zip") from None
            if len(content) > limit:
                raise Refused(f"{path} is larger than {limit} bytes")
            if path == MANIFEST:
                manifest = content
                continue
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise Refused(f"the skill holds more than {MAX_TOTAL_BYTES} bytes")
            files[path] = content
    if manifest is None:
        raise Refused(f"the zip has no {MANIFEST}")
    listed_revision, listed = parse_manifest(manifest, skill)
    if listed_revision != revision:
        raise Refused(f"{MANIFEST} says revision {short(listed_revision)}, not {short(revision)}")
    if set(listed) != set(files):
        extra = sorted(set(files) - set(listed)) + sorted(set(listed) - set(files))
        raise Refused(f"the zip and {MANIFEST} disagree about {extra[0]}")
    for path, content in files.items():
        digest, size = listed[path]
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise Refused(f"{path} does not match its sha256 in {MANIFEST}")
    return files, manifest


@dataclass
class Installed:
    name: str
    root: Path
    revision: str | None                         # None when the manifest cannot be read
    listed: dict[str, tuple[str, int]] | None

    @property
    def path(self) -> Path:
        return self.root / self.name


def load_installed(root: Path, name: str) -> Installed | None:
    """The managed skill `root/name`, or None when that directory is not one."""
    path = root / name
    manifest = path / MANIFEST
    if path.is_symlink() or not path.is_dir() or not manifest.is_file():
        return None
    try:
        raw = manifest.read_bytes()[:MAX_MANIFEST_BYTES + 1]
        revision, listed = parse_manifest(raw, name)
    except (OSError, Refused):
        return Installed(name, root, None, None)
    return Installed(name, root, revision, listed)


def installed_skills(roots: list[Path]) -> list[Installed]:
    found = []
    for root in roots:
        try:
            names = sorted(entry.name for entry in root.iterdir())
        except OSError:
            continue
        for name in names:
            if valid_name(name):
                skill = load_installed(root, name)
                if skill is not None:
                    found.append(skill)
    return found


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def local_changes(skill: Installed) -> list[str]:
    """How the installed files differ from the installed manifest: each file modified, added or
    removed. Python caches and Finder's .DS_Store are not changes."""
    if skill.listed is None:
        return [f"{MANIFEST} cannot be read"]
    changes, present = [], set()
    for dirpath, dirnames, filenames in os.walk(skill.path):
        here = Path(dirpath)
        for name in list(dirnames):
            if name == "__pycache__":
                dirnames.remove(name)
            elif (here / name).is_symlink():
                dirnames.remove(name)
                changes.append(f"{(here / name).relative_to(skill.path).as_posix()} added")
        for name in filenames:
            full = here / name
            rel = full.relative_to(skill.path).as_posix()
            if rel == MANIFEST or name == ".DS_Store":
                continue
            if rel not in skill.listed or full.is_symlink() or not full.is_file():
                changes.append(f"{rel} added")
                continue
            present.add(rel)
            try:
                if _sha256_file(full) != skill.listed[rel][0]:
                    changes.append(f"{rel} modified")
            except OSError:
                changes.append(f"{rel} unreadable")
    changes += [f"{rel} removed" for rel in set(skill.listed) - present]
    return sorted(changes)


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def install(root: Path, skill: str, files: dict[str, bytes], manifest: bytes) -> None:
    """Write the package beside `root/skill`, then swap it in: the current copy moves to
    root/.slancha-backup/skill (one backup per skill, replacing the previous one) and the new
    directory is renamed into place. Both renames stay on one filesystem."""
    root.mkdir(parents=True, exist_ok=True)
    # A dot-directory that holds no SKILL.md at its top, so Claude Code never loads the staging
    # copy as a skill of its own.
    staging = Path(tempfile.mkdtemp(prefix=".slancha-tmp-", dir=root))
    try:
        new = staging / skill
        for rel, content in list(files.items()) + [(MANIFEST, manifest)]:
            target = new.joinpath(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(target, flags, 0o644), "wb") as handle:
                handle.write(content)
        current = root / skill
        if not (current.exists() or current.is_symlink()):
            os.rename(new, current)
            return
        backups = root / BACKUP_DIR
        backups.mkdir(exist_ok=True)
        ignore = backups / ".gitignore"          # a project's .claude/skills is often in git
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        backup = backups / skill
        if backup.exists() or backup.is_symlink():
            _remove(backup)
        os.rename(current, backup)
        try:
            os.rename(new, current)
        except OSError:
            os.rename(backup, current)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# --- update --------------------------------------------------------------------------------------

def update(cfg: Config, project: str, skill: str | None, dry_run: bool, force: bool) -> list[str]:
    """The report lines for /slancha:update."""
    roots = skill_roots(project)
    managed = installed_skills(roots)
    targets = [s for s in managed if skill is None or s.name == skill]
    if cfg.problem:
        return [cfg.problem]
    if skill is None and not managed:
        head = ["No Slancha skills are installed here."]
    else:
        head = []
    library = fetch_library(cfg)
    lines = []
    if skill is not None and not targets:
        lines.append(install_new(cfg, roots[0], skill, library.get(skill), dry_run, force))
    for target in targets:
        lines.append(update_one(cfg, target, library.get(target.name), dry_run, force))
    if skill is None:
        lines += not_installed(library, managed) or (
            [] if managed else ["Your Slancha library has no published skills yet."])
    return head + lines


def not_installed(library: dict[str, dict], managed: list[Installed]) -> list[str]:
    names = {s.name for s in managed}
    more = sorted(name for name, row in library.items() if name not in names and published(row))
    return [f"Also in your library: {', '.join(more)} (install one with /slancha:update <skill>)"
            ] if more else []


def install_new(cfg: Config, root: Path, skill: str, row: dict | None, dry_run: bool,
                force: bool) -> str:
    revision = published(row)
    if row is None:
        return f"{skill}: not in your Slancha library."
    if revision is None:
        return f"{skill}: not published yet (it is waiting for review in the console)."
    target = root / skill
    if target.exists() or target.is_symlink():
        if not force:
            return (f"{skill}: {shown(target)} exists and was not installed by Slancha; move it "
                    "away, or rerun with --force to replace it (it is kept in "
                    f"{shown(root / BACKUP_DIR)}).")
    if dry_run:
        return f"{skill}: would install {short(revision)} in {shown(root)}."
    root_existed = root.is_dir()
    try:
        files, manifest = unpack(download(cfg, skill, revision), skill, revision)
        install(root, skill, files, manifest)
    except Refused as exc:
        return f"{skill}: refused the download, nothing installed: {exc}."
    except OSError as exc:
        return f"{skill}: could not write {shown(target)} ({exc.strerror or exc})."
    line = f"{skill}: installed {short(revision)} in {shown(root)}."
    if not root_existed:
        # Claude Code watches skill directories that existed when the session started.
        line += f" Run /reload-skills to load it: Claude Code was not watching {shown(root)} yet."
    return line


def update_one(cfg: Config, skill: Installed, row: dict | None, dry_run: bool,
               force: bool) -> str:
    name, where = skill.name, shown(skill.root)
    revision = published(row)
    if revision is None:
        state = ("not in your Slancha library" if row is None else
                 "not published yet (it is waiting for review in the console)")
        return f"{name}: {state}; left as it is ({where})."
    if revision == skill.revision:
        return f"{name}: up to date ({short(revision)})."
    changes = local_changes(skill)
    before = short(skill.revision) if skill.revision else "unknown"
    if changes and not force:
        listed = ", ".join(changes[:3]) + (f" and {len(changes) - 3} more" if len(changes) > 3
                                           else "")
        verb = "would keep" if dry_run else "kept"
        return (f"{name}: {verb} {before}, it has local changes ({listed}). Rerun with --force to "
                f"replace them; the current copy goes to {shown(skill.root / BACKUP_DIR)}.")
    if dry_run:
        return f"{name}: would update {before} -> {short(revision)} ({where})."
    try:
        files, manifest = unpack(download(cfg, name, revision), name, revision)
        install(skill.root, name, files, manifest)
    except Refused as exc:
        return f"{name}: refused the download, kept {before}: {exc}."
    except OSError as exc:
        return f"{name}: could not replace {shown(skill.path)} ({exc.strerror or exc}); kept {before}."
    return f"{name}: updated {before} -> {short(revision)} ({where})."


# --- status --------------------------------------------------------------------------------------

def status(cfg: Config, project: str) -> list[str]:
    lines = [f"Slancha plugin {VERSION}, API {cfg.api_url or '(invalid)'}"
             + (f", token from {cfg.token_source}" if cfg.token else "")]
    if cfg.token and cfg.token_source.startswith("~"):
        try:
            if (config_dir() / "token").stat().st_mode & 0o077:
                lines.append("Warning: other users can read ~/.config/slancha/token; run "
                             "chmod 600 ~/.config/slancha/token")
        except OSError:
            pass
    managed = installed_skills(skill_roots(project))
    library, offline = None, None
    if cfg.problem:
        lines.append(cfg.problem)
    else:
        try:
            library = fetch_library(cfg)
            me = api_json(cfg, "GET", "/api/me", timeout=10)
            if isinstance(me, dict):
                lines.append(f"{token_line(me.get('capabilities'), me.get('expires_at'))[:-1]}; "
                             f"role {printable(me.get('role'), 40)}.")
        except SlanchaError as exc:
            if library is None:
                offline = str(exc)
    for skill in managed:
        state = ""
        if skill.revision is None:
            state = f"{MANIFEST} cannot be read"
        elif library is not None:
            revision = published(library.get(skill.name))
            state = ("not in your library" if revision is None
                     else "up to date" if revision == skill.revision
                     else f"update available: {short(revision)}")
        if skill.revision and local_changes(skill):
            state = (state + ", " if state else "") + "local changes"
        lines.append(f"  {skill.name}  {short(skill.revision) if skill.revision else '?'}  "
                     f"{shown(skill.root)}" + (f"  {state}" if state else ""))
    if not managed:
        lines.append("No Slancha skills are installed here.")
    if library is not None:
        lines += not_installed(library, managed)
    if offline:
        lines.append(offline)
    waiting = len(read_spool())
    last = read_json(cache_dir() / "last-flush.json")
    usage = f"Usage: {waiting} event{'s' if waiting != 1 else ''} waiting to upload"
    if isinstance(last, dict) and isinstance(last.get("at"), (int, float)):
        usage += f"; last upload attempt {ago(last['at'])}: {last.get('result', '?')}"
    lines.append(usage + ".")
    return lines


def ago(at: float) -> str:
    minutes = int(max(0, time.time() - at) // 60)
    if minutes < 1:
        return "just now"
    if minutes < 120:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago" if minutes < 48 * 60 else f"{minutes // 1440} days ago"


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- usage ---------------------------------------------------------------------------------------

@contextlib.contextmanager
def locked(path: Path, blocking: bool = True):
    """An exclusive flock on `path`; yields False when non-blocking and already held."""
    if fcntl is None:
        yield True
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _cache() -> Path:
    path = cache_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def read_spool() -> list[dict]:
    try:
        text = (cache_dir() / "usage.jsonl").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def sendable(event: dict, now: float) -> bool:
    """The server's own checks, so one bad event cannot get a whole batch refused."""
    at = event.get("at")
    return (isinstance(event.get("id"), str) and bool(_EVENT_ID.fullmatch(event["id"]))
            and isinstance(event.get("skill"), str) and bool(_USAGE_SKILL.fullmatch(event["skill"]))
            and isinstance(event.get("revision"), str)
            and (event["revision"] == "unknown" or bool(_USAGE_REVISION.fullmatch(event["revision"])))
            and event.get("kind") in USAGE_KINDS and event.get("success") in (True, False, None)
            and isinstance(event.get("session"), str) and bool(REVISION.fullmatch(event["session"]))
            and isinstance(at, (int, float)) and not isinstance(at, bool)
            and now - MAX_EVENT_AGE_S <= at <= now + 300)


def pending_events(events: list[dict], now: float) -> list[dict]:
    """Sendable events, one per id, oldest first, at most the newest MAX_SPOOL_EVENTS."""
    unique: dict[str, dict] = {}
    for event in events:
        if sendable(event, now):
            unique.setdefault(event["id"].lower(), event)
    ordered = sorted(unique.values(), key=lambda e: e["at"])
    return ordered[-MAX_SPOOL_EVENTS:]


def append_event(event: dict) -> None:
    cache = _cache()
    with locked(cache / "spool.lock"):
        fd = os.open(cache / "usage.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")


def flush(cfg: Config) -> str:
    """Upload the spool. What the server stored, and what it can never accept, leaves the spool;
    everything else stays for the next attempt."""
    cache = _cache()
    with locked(cache / "flush.lock", blocking=False) as mine:
        if not mine:
            return "another upload is running"
        with locked(cache / "spool.lock"):
            pending = pending_events(read_spool(), time.time())
        done: set[str] = set()     # ids the server stored, or refused for good
        sent, result = 0, "nothing to upload"
        if pending and cfg.problem:
            result = "not sent: " + cfg.problem.split(". ")[0]
        elif pending:
            result = ""
            for start in range(0, len(pending), BATCH):
                batch = pending[start:start + BATCH]
                ids = {e["id"].lower() for e in batch}
                try:
                    request(cfg, "POST", "/api/usage/skills", body={"events": batch}, timeout=15)
                except Expired as exc:
                    note_expired()
                    result = f"kept for later: {exc}"
                    break
                except ApiError as exc:
                    if exc.status == 400:            # never acceptable; retrying cannot help
                        done |= ids
                        result = f"dropped {len(batch)} refused events (HTTP 400)"
                        continue
                    result = f"kept for later: {exc}"
                    break
                except SlanchaError as exc:
                    result = f"kept for later: {exc}"
                    break
                done |= ids
                sent += len(batch)
            result = f"sent {sent} event{'s' if sent != 1 else ''}" + (f"; {result}" if result
                                                                     else "")
        with locked(cache / "spool.lock"):     # events recorded meanwhile are kept
            keep = [e for e in pending_events(read_spool(), time.time())
                    if e["id"].lower() not in done]
            spool = cache / "usage.jsonl"
            if keep:
                tmp = cache / f"usage.jsonl.{os.getpid()}.tmp"
                with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
                               encoding="utf-8") as handle:
                    handle.writelines(json.dumps(e, sort_keys=True) + "\n" for e in keep)
                os.replace(tmp, spool)
            elif spool.exists():
                spool.unlink()
        state = cache / "last-flush.json"
        tmp = cache / f"last-flush.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"at": int(time.time()), "result": result}), encoding="utf-8")
        os.replace(tmp, state)
        return result


def start_flush() -> None:
    """Start `flush` detached from Claude Code: SessionEnd hooks get about 1.5 s, and a `-p`
    session kills its hooks at exit, so the upload must not live inside the hook."""
    try:
        if (cache_dir() / "usage.jsonl").stat().st_size == 0:
            return
    except OSError:
        return
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "flush"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                     cwd=str(Path.home()))


def frontmatter_name(skill: Installed) -> str | None:
    try:
        with open(skill.path / "SKILL.md", encoding="utf-8", errors="replace") as handle:
            lines = [handle.readline() for _ in range(60)]
    except OSError:
        return None
    if lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        found = re.match(r"name:\s*['\"]?([^'\"#]+?)['\"]?\s*$", line)
        if found:
            return found.group(1)
    return None


def find_managed(name, project: str) -> Installed | None:
    """The managed skill Claude Code runs for `/name` or Skill(name): the personal directory wins
    over the project's, and a name may also be a skill's frontmatter `name`. Plugin skills
    (`plugin:skill`) are never managed."""
    if not isinstance(name, str):
        return None
    name = name.strip().lstrip("/")
    roots = skill_roots(project)
    if valid_name(name):
        for root in roots:
            if (root / name).is_dir():
                return load_installed(root, name)
    if not name or ":" in name or "/" in name:
        return None
    for skill in installed_skills(roots):
        if frontmatter_name(skill) == name:
            return skill
    return None


def record(event: dict, name, success, source) -> None:
    """Spool one `invoked` event for a managed skill. It holds no content: the skill, the
    installed revision ("unknown" once the files were edited), success, the sha256 of the
    session id and the time. The id derives from the session and the prompt or tool call, so a
    hook that fires twice produces one event."""
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    skill = find_managed(name, project_dir(event=event))
    if skill is None:
        return
    session = hashlib.sha256(session_id.encode()).hexdigest()
    event_id = (uuid.uuid5(_EVENT_NAMESPACE, f"{session}:{source}:{skill.name}")
                if isinstance(source, str) and source else uuid.uuid4())
    revision = skill.revision if skill.revision and not local_changes(skill) else "unknown"
    append_event({"id": str(event_id), "skill": skill.name, "revision": revision,
                  "kind": "invoked", "success": success, "session": session,
                  "at": int(time.time())})


def note_expired() -> None:
    """Leave one notice for the next session start, however many uploads find the token
    expired."""
    with contextlib.suppress(OSError):
        (_cache() / "token-expired").touch()


def session_notice(event: dict) -> str | None:
    """That the token expired, when an upload found it so; else at most once a day, which
    installed skills have a newer published revision."""
    stamp = cache_dir() / "update-check.stamp"
    try:
        (cache_dir() / "token-expired").unlink()
    except OSError:
        pass
    else:
        stamp.touch()                 # today's update check would only find the same
        return TOKEN_EXPIRED
    try:
        if time.time() - stamp.stat().st_mtime < 86400:
            return None
    except OSError:
        pass
    cfg = load_config()
    if cfg.problem:
        return None
    managed = [s for s in installed_skills(skill_roots(project_dir(event=event))) if s.revision]
    if not managed:
        return None
    _cache()
    stamp.touch()
    try:
        library = fetch_library(cfg, timeout=3)
    except Expired:
        return TOKEN_EXPIRED
    stale = sorted({s.name for s in managed
                    if published(library.get(s.name)) not in (None, s.revision)})
    if not stale:
        return None
    return f"Slancha: updates available for {', '.join(stale)}. Run /slancha:update to install."


def hook(stdin: bytes) -> str | None:
    """Handle one Claude Code hook event. Returns what to print (only SessionStart prints)."""
    event = json.loads(stdin)
    if not isinstance(event, dict):
        return None
    name = event.get("hook_event_name")
    if name in ("PostToolUse", "PostToolUseFailure") and event.get("tool_name") == "Skill":
        tool_input = event.get("tool_input")
        response = event.get("tool_response")
        success = name == "PostToolUse" and not (isinstance(response, dict)
                                                 and response.get("success") is False)
        if isinstance(tool_input, dict):
            record(event, tool_input.get("skill"), success, event.get("tool_use_id"))
    elif name == "UserPromptExpansion" and event.get("expansion_type") == "slash_command":
        record(event, event.get("command_name"), None, event.get("prompt_id"))
    elif name in ("Stop", "SessionEnd"):
        start_flush()
    elif name == "SessionStart":
        notice = session_notice(event)
        if notice:
            return json.dumps({"systemMessage": notice})
    return None


# --- auth: the device authorization grant --------------------------------------------------------

EXPIRED = "The code expired before an admin approved it. Nothing was saved; run slancha auth again."


def say(*lines: str) -> None:
    for line in lines:
        print(line, flush=True)       # stdout is a pipe under install.sh and in tests


def printable(value, limit: int = 300) -> str:
    return "".join(c if c.isprintable() else " " for c in str(value))[:limit]


def post_public(api: str, path: str, body: dict, timeout: float = 20) -> tuple[int, dict]:
    """(status, JSON object or {}) of an unauthenticated POST. Redirects are refused."""
    req = urllib.request.Request(api + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json",
                                          "User-Agent": f"slancha-claude-plugin/{VERSION}"})
    host = urllib.parse.urlsplit(api).netloc
    try:
        with _OPENER.open(req, timeout=timeout) as response:
            status, payload = response.status, response.read(65537)
    except urllib.error.HTTPError as error:
        with contextlib.closing(error):
            if 300 <= error.code < 400:
                raise ApiError(error.code, f"{host} answered with a redirect (HTTP {error.code}); "
                                           "not following it.") from None
            status, payload = error.code, error.read(65537)
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        reason = getattr(error, "reason", None) or error.__class__.__name__
        raise Offline(f"Cannot reach {host} ({reason}).") from None
    try:
        doc = json.loads(payload)
    except ValueError:
        doc = None
    return status, doc if isinstance(doc, dict) else {}


def answered(status: int, doc: dict, api: str) -> str:
    detail = doc.get("detail") or doc.get("error")
    if not isinstance(detail, str):
        hint = WRONG_API.format(host=urllib.parse.urlsplit(api).netloc) if status in (404, 429) else ""
        return f"Slancha answered HTTP {status}.{hint}"
    return f"Slancha answered HTTP {status}: {printable(detail, 200)}"


def read_config_json() -> dict:
    try:
        doc = json.loads((config_dir() / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict):
        raise SlanchaError("~/.config/slancha/config.json is not a JSON object; fix or remove it.")
    return doc


def write_private(path: Path, text: str) -> None:
    """Replace `path` atomically with a file only the user can read (0600)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def start_device(api: str, tracing: bool, manage: bool) -> dict:
    client = f"slancha CLI on {socket.gethostname().split('.')[0] or 'an unnamed host'}"
    body = {"client": client, **({"tracing": True} if tracing else {}),
            **({"manage": True} if manage else {})}
    status, doc = post_public(api, "/api/cli/device", body)
    if status != 200:
        raise ApiError(status, answered(status, doc, api))
    uri = doc.get("verification_uri_complete")
    parts = urllib.parse.urlsplit(uri) if isinstance(uri, str) else None
    ok = (isinstance(doc.get("device_code"), str) and doc["device_code"]
          and isinstance(doc.get("user_code"), str) and doc["user_code"]
          and parts is not None and (parts.scheme == "https" or (
              parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")))
          and isinstance(doc.get("expires_in"), int) and doc["expires_in"] > 0
          and isinstance(doc.get("interval", 5), int))
    if not ok:
        raise SlanchaError("Slancha answered /api/cli/device without a usable device code.")
    return doc


def open_browser(url: str) -> None:
    # Without a display, Linux's webbrowser falls back to a text browser that takes over the
    # terminal while the code is waiting; print the link only.
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY")
                                                 or os.environ.get("WAYLAND_DISPLAY")):
        return
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


def wait_for_approval(api: str, device_code: str, expires_in: int, interval: int) -> dict:
    """Poll /api/cli/token until an admin approves the code (RFC 8628, section 3.5)."""
    deadline = time.monotonic() + expires_in
    interval, failures = max(1, interval), 0
    while time.monotonic() + interval < deadline:
        time.sleep(interval)
        try:
            status, doc = post_public(api, "/api/cli/token", {"device_code": device_code})
        except Offline:
            status, doc = 0, {}
        if status == 0 or status >= 500:           # a blip; the code stays valid until it expires
            failures += 1
            if failures >= 5:
                raise SlanchaError("Slancha stopped answering while waiting for approval. Nothing "
                                   "was saved; run slancha auth again.")
            continue
        failures = 0
        error = doc.get("error")
        if status == 200:
            return doc
        if status == 400 and error == "authorization_pending":
            continue
        if status == 400 and error == "slow_down":
            interval += 5
            continue
        if status == 400 and error == "expired_token":
            raise SlanchaError(EXPIRED)
        if status == 400 and error == "access_denied":
            raise SlanchaError("The request was denied in the Slancha console. Nothing was saved.")
        raise ApiError(status, answered(status, doc, api) + " Nothing was saved.")
    raise SlanchaError(EXPIRED)


def token_line(capabilities, expires_at) -> str:
    caps = [printable(c, 40) for c in capabilities if isinstance(c, str)] \
        if isinstance(capabilities, list) else []
    expiry = (f"expires {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(expires_at))} "
              f"(in {max(0, expires_at - time.time()) / 86400:.0f} days)"
              if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool)
              else "does not expire")
    return f"Token: {', '.join(caps) or 'capabilities unknown'}; {expiry}."


def auth(api_arg: str | None, browser: bool, tracing_wanted: bool, manage: bool) -> None:
    api = check_url(api_arg, "--api") if api_arg else api_base()
    settings = read_config_json()        # checked now: a broken file must not cost an approval
    device = start_device(api, tracing_wanted, manage)
    uri = printable(device["verification_uri_complete"], 1000)
    minutes = max(1, device["expires_in"] // 60)
    who = ("An admin of your Slancha org approves this request on that page, because it asks for "
           "the org's tracing keys" if tracing_wanted else
           "Approve this request on that page, signed in to your Slancha org as a proposer, "
           "approver or admin" if manage else
           "Approve this request on that page, signed in to your Slancha org: the token is yours")
    say(f"Signing in to Slancha at {api}.", "",
        f"  Your code:  {printable(device['user_code'], 40)}",
        f"  Approve at: {uri}", "",
        f"{who} (check that it shows the same code).",
        *(["It asks for an agent token: it acts as whoever approves it, at most as a proposer "
           "(upload, check, scan and draft; never approve), and expires after 30 days."]
          if manage else []),
        f"Waiting up to {minutes} min for the approval; Ctrl-C stops.")
    if browser:
        open_browser(device["verification_uri_complete"])
    doc = wait_for_approval(api, device["device_code"], device["expires_in"],
                            device.get("interval", 5))
    token, tracing = doc.get("token"), doc.get("tracing")
    if not (isinstance(token, str) and TOKEN.fullmatch(token)):
        raise SlanchaError("Slancha approved the request but sent no personal token. Nothing was "
                           "saved.")
    keys = ("host", "public_key", "secret_key")
    if tracing is not None:
        if not (isinstance(tracing, dict) and all(isinstance(tracing.get(k), str)
                                                  and tracing[k].strip() for k in keys)):
            raise SlanchaError("Slancha sent unusable tracing keys. Nothing was saved.")
        tracing = {k: tracing[k].strip() for k in keys}
        tracing["host"] = check_url(tracing["host"], "The tracing host Slancha sent")

    directory = config_dir()
    old = os.umask(0o077)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        write_private(directory / "token", token)
        write_private(directory / "config.json",
                      json.dumps({**settings, "api_url": api}, indent=2) + "\n")
        stale = False
        if tracing is not None:
            write_private(directory / "tracing.json", json.dumps(tracing, indent=2) + "\n")
        elif (directory / "tracing.json").exists():
            (directory / "tracing.json").unlink()    # it belonged to an earlier sign-in
            stale = True
    finally:
        os.umask(old)
    with contextlib.suppress(OSError):
        (cache_dir() / "token-expired").unlink()       # about the token just replaced

    org = doc.get("org_id")
    lines = ["", "Approved" + (f" for org {printable(org, 80)}." if isinstance(org, str) else "."),
             "Saved, readable only by you:",
             "  ~/.config/slancha/token        your " + ("agent token" if manage
                                                        else "personal token"),
             f"  ~/.config/slancha/config.json  api_url {api}"]
    if tracing is not None:
        lines.append("  ~/.config/slancha/tracing.json your org's tracing keys "
                     f"({tracing['host']})")
    capabilities = doc.get("capabilities")
    lines.append(token_line(capabilities, doc.get("expires_at")))
    if manage and not (isinstance(capabilities, list) and "manage" in capabilities):
        lines.append("Note: Slancha sent a token without the manage capability.")
    if manage:
        lines.append("It replaces the token the plugin's hooks use too: once it expires, run "
                     "slancha auth again.")
    if tracing is not None:
        lines.append("Next: slancha init --tracing")
    else:
        if stale:
            lines.append("Removed ~/.config/slancha/tracing.json from an earlier sign-in (tracing "
                         "already set up in Claude Code stays as it is).")
        lines.append("Slancha sent no tracing keys: ask an org admin to connect tracing in the "
                     "console, then run slancha auth --tracing again." if tracing_wanted
                     else "Next: slancha init")
    if os.environ.get("SLANCHA_TOKEN"):
        lines.append("Note: SLANCHA_TOKEN is set in this shell and takes precedence over the "
                     "saved token; unset it to use the new one.")
    say(*lines)


# --- init: configure Claude Code on this machine -------------------------------------------------

LANGFUSE_REPO = "https://github.com/langfuse/claude-observability-plugin"
LANGFUSE_COMMIT = "b5211009698fdfe79c6bbe90d6901ad255ea65d3"
LANGFUSE_MARKETPLACE = "langfuse-observability"
LANGFUSE_PLUGIN = f"langfuse-observability@{LANGFUSE_MARKETPLACE}"
# Non-secret options only: skill tags on, image upload off. No trace seed.
LANGFUSE_OPTIONS = {"CC_LANGFUSE_SKILL_TAGS": True, "CC_LANGFUSE_CAPTURE_IMAGES": False}
# The Langfuse SDK's own names. The hook reads them before the CC_ names, and every process Claude
# Code starts inherits settings `env`, so they would reroute other programs' traces.
SDK_ENV = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "LANGFUSE_BASE_URL")
SLANCHA_REPO = "SlanchaAI/slancha-claude-plugin"
SLANCHA_MARKETPLACE = "slancha"        # the name in .claude-plugin/marketplace.json; a test checks
SLANCHA_PLUGIN = f"slancha@{SLANCHA_MARKETPLACE}"


def langfuse_dir() -> Path:
    return Path.home() / ".local" / "share" / "slancha" / "langfuse-observability"


def run(cmd: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SlanchaError(f"`{shlex.join(cmd)}` did not run: {exc}") from None


def failed(cmd: list[str], result: subprocess.CompletedProcess) -> SlanchaError:
    tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
    return SlanchaError(f"`{shlex.join(cmd)}` failed (exit {result.returncode})"
                        + (": " + " / ".join(printable(t) for t in tail) if tail else "."))


def must(cmd: list[str]) -> str:
    result = run(cmd)
    if result.returncode != 0:
        raise failed(cmd, result)
    return result.stdout


def prerequisites(tracing: bool) -> list[str]:
    """What is missing for init; uv and its Python only matter to the Langfuse hook."""
    missing = []
    tools = [("claude", "Claude Code"), ("git", "git")]
    if tracing:
        tools.append(("uv", "uv (https://docs.astral.sh/uv/), which runs the Langfuse hook"))
    for tool, what in tools:
        if shutil.which(tool) is None:
            missing.append(f"`{tool}` is not on PATH: install {what}.")
    if tracing and shutil.which("uv") and run(["uv", "python", "find", ">=3.10"]).returncode != 0:
        missing.append("uv finds no Python 3.10 or newer, which the Langfuse hook needs: run "
                       "`uv python install 3.12`.")
    return missing


def load_tracing() -> dict:
    doc = read_json(config_dir() / "tracing.json")
    if not (isinstance(doc, dict) and all(isinstance(doc.get(k), str) and doc[k]
                                          for k in ("host", "public_key", "secret_key"))):
        raise SlanchaError("No tracing keys in ~/.config/slancha/tracing.json: run slancha auth "
                           "--tracing first; an org admin approves it.")
    doc["host"] = check_url(doc["host"], "host in ~/.config/slancha/tracing.json")
    return doc


def json_list(cmd: list[str]) -> list[dict]:
    try:
        rows = json.loads(must(cmd))
    except ValueError:
        rows = None
    if not isinstance(rows, list):
        raise SlanchaError(f"`{shlex.join(cmd)}` did not print a JSON list.")
    return [row for row in rows if isinstance(row, dict)]


class Actions:
    """Runs each change, or with --dry-run only says what it would run."""

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run

    def run(self, cmd: list[str]) -> None:
        home = str(Path.home()) + os.sep
        shown_cmd = " ".join("~/" + shlex.quote(c[len(home):]) if c.startswith(home)
                             else shlex.quote(c) for c in cmd)
        say(f"  {'would run' if self.dry_run else 'running'}: {shown_cmd}")
        if not self.dry_run:
            must(cmd)


def git_head(repo: Path) -> str:
    result = run(["git", "-C", str(repo), "rev-parse", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else ""


def setup_langfuse(act: Actions, installed: set[str], markets: dict[str, dict]) -> None:
    repo = langfuse_dir()
    say(f"Langfuse's Claude Code plugin, pinned to {LANGFUSE_COMMIT[:8]}:")
    if repo.exists() and not (repo / ".git").exists():
        raise SlanchaError(f"{shown(repo)} exists but is not a git clone; move it away and run "
                           "slancha init again.")
    if not repo.exists():
        if not act.dry_run:
            repo.parent.mkdir(parents=True, exist_ok=True)
        act.run(["git", "clone", "--quiet", LANGFUSE_REPO, str(repo)])
    if act.dry_run and not repo.exists():
        act.run(["git", "-C", str(repo), "checkout", "--quiet", "--detach", LANGFUSE_COMMIT])
    elif git_head(repo) != LANGFUSE_COMMIT:
        if run(["git", "-C", str(repo), "cat-file", "-e",
                LANGFUSE_COMMIT + "^{commit}"]).returncode != 0:
            act.run(["git", "-C", str(repo), "fetch", "--quiet", "origin"])
        act.run(["git", "-C", str(repo), "checkout", "--quiet", "--detach", LANGFUSE_COMMIT])
        if not act.dry_run and git_head(repo) != LANGFUSE_COMMIT:
            raise SlanchaError(f"{shown(repo)} is not at {LANGFUSE_COMMIT} after the checkout; "
                               "move it away and run slancha init again.")
    else:
        say(f"  {shown(repo)} is at {LANGFUSE_COMMIT[:8]}")

    market = markets.get(LANGFUSE_MARKETPLACE)
    if market is None:
        act.run(["claude", "plugin", "marketplace", "add", str(repo)])
    else:
        source = market.get("path") or market.get("installLocation") or market.get("repo") or "?"
        if not (isinstance(source, str)
                and os.path.realpath(source) == os.path.realpath(repo)):
            raise SlanchaError(
                f"Claude Code already has a marketplace named {LANGFUSE_MARKETPLACE} from "
                f"{printable(source)}, not the pinned clone. Remove it with `claude plugin "
                f"marketplace remove {LANGFUSE_MARKETPLACE}` (this also uninstalls its plugin), "
                "then run slancha init again.")
        say(f"  marketplace {LANGFUSE_MARKETPLACE} is the pinned clone")
    if LANGFUSE_PLUGIN in installed:
        say(f"  {LANGFUSE_PLUGIN} is installed")
    else:
        act.run(["claude", "plugin", "install", LANGFUSE_PLUGIN, "--scope", "user"])


def setup_slancha(act: Actions, installed: set[str], markets: dict[str, dict]) -> None:
    say("The Slancha plugin:")
    if SLANCHA_MARKETPLACE in markets:
        say(f"  marketplace {SLANCHA_MARKETPLACE} is configured")
    else:
        act.run(["claude", "plugin", "marketplace", "add", SLANCHA_REPO])
    if SLANCHA_PLUGIN in installed:
        say(f"  {SLANCHA_PLUGIN} is installed")
    else:
        act.run(["claude", "plugin", "install", SLANCHA_PLUGIN, "--scope", "user"])


def mentions_langfuse(node) -> bool:
    if isinstance(node, str):
        return "langfuse" in node.lower()
    if isinstance(node, dict):
        return any(mentions_langfuse(v) for v in node.values())
    if isinstance(node, list):
        return any(mentions_langfuse(v) for v in node)
    return False


def merge_settings(settings: dict, tracing: dict) -> list[str]:
    """Merge the tracing configuration into Claude Code's settings in place; what changed."""
    notes = []
    env = settings.setdefault("env", {})
    configs = settings.setdefault("pluginConfigs", {})
    entry = configs.setdefault(LANGFUSE_PLUGIN, {}) if isinstance(configs, dict) else None
    options = entry.setdefault("options", {}) if isinstance(entry, dict) else None
    if not isinstance(env, dict) or not isinstance(options, dict):
        raise SlanchaError("settings.json has an `env` or `pluginConfigs` that is not an object; "
                           "fix it, then run slancha init again.")
    wanted = {"CC_LANGFUSE_PUBLIC_KEY": tracing["public_key"],
              "CC_LANGFUSE_SECRET_KEY": tracing["secret_key"],
              "CC_LANGFUSE_BASE_URL": tracing["host"]}
    changed = [k for k, v in wanted.items() if env.get(k) != v]
    env.update(wanted)
    if changed:
        notes.append(f"set env {', '.join(changed)} (Langfuse host {tracing['host']})")
    removed = [k for k in SDK_ENV if k in env]
    for key in removed:
        del env[key]
    if removed:
        notes.append(f"remove env {', '.join(removed)}: the Langfuse SDK's own names would "
                     "reroute the traces of every program Claude Code starts; the plugin reads "
                     "the CC_LANGFUSE_* names instead")
    changed = [k for k, v in LANGFUSE_OPTIONS.items() if options.get(k) != v]
    options.update(LANGFUSE_OPTIONS)
    if changed:
        notes.append(f"set pluginConfigs[\"{LANGFUSE_PLUGIN}\"].options "
                     + ", ".join(f"{k}={json.dumps(LANGFUSE_OPTIONS[k])}" for k in changed))
    return notes


def setup_settings(act: Actions, tracing: dict) -> None:
    path = claude_config_dir() / "settings.json"
    say(f"Claude Code settings ({shown(path)}):")
    real = Path(os.path.realpath(path))
    try:
        before = real.read_text(encoding="utf-8")
        settings = json.loads(before)
    except FileNotFoundError:
        before, settings = None, {}
    except (OSError, ValueError):
        settings = None
    if not isinstance(settings, dict):
        raise SlanchaError(f"{shown(path)} is not a JSON object; fix it, then run slancha init "
                           "again.")
    if mentions_langfuse(settings.get("hooks")):
        say("  Warning: settings.json has a hook that runs Langfuse. With the plugin installed "
            "every turn would be sent twice; remove that hook. (slancha init adds none.)")
    notes = merge_settings(settings, tracing)
    after = json.dumps(settings, indent=2) + "\n"
    private = before is not None and not real.stat().st_mode & 0o077
    for note in notes:
        note = note if act.dry_run else note.replace("remove env", "removed env", 1)
        say(f"  {'would ' if act.dry_run else ''}{note}")
    if after == before and private:
        say("  already configured")
        return
    if act.dry_run:
        say("  would write it atomically with mode 0600, because it would hold your org's "
            "Langfuse secret key")
        return
    real.parent.mkdir(parents=True, exist_ok=True)
    write_private(real, after)
    say("  wrote it atomically with mode 0600 (readable only by you), because it now holds your "
        "org's Langfuse secret key")


def check(tracing: bool) -> None:
    """What init needs on this machine, before anything else. install.sh runs it ahead of slancha
    auth, so a missing tool is found before anyone has approved a sign-in for it."""
    missing = prerequisites(tracing)
    if missing:
        raise SlanchaError("\n".join(["Slancha needs, on this machine:"]
                                     + [f"  - {m}" for m in missing]))
    say("This machine has what slancha init --tracing needs: claude, git, and uv with Python 3.10 "
        "or newer." if tracing else "This machine has what slancha init needs: claude and git.")


def init(dry_run: bool, tracing: bool) -> None:
    """The Slancha plugin; with `tracing`, also Langfuse's plugin and the org's tracing keys.
    Without it nothing of tracing is touched, so tracing set up earlier stays as it is."""
    cfg = load_config()
    if cfg.token is None:
        raise SlanchaError("No Slancha token on this machine: run slancha auth first.")
    keys = load_tracing() if tracing else None
    missing = prerequisites(tracing)
    if missing:
        raise SlanchaError("\n".join(["slancha init needs:"] + [f"  - {m}" for m in missing]))
    act = Actions(dry_run)
    if dry_run:
        say("Dry run: nothing is changed.")
    installed = {row["id"] for row in json_list(["claude", "plugin", "list", "--json"])
                 if isinstance(row.get("id"), str)}
    markets = {row["name"]: row
               for row in json_list(["claude", "plugin", "marketplace", "list", "--json"])
               if isinstance(row.get("name"), str)}
    if keys is not None:
        setup_langfuse(act, installed, markets)
    setup_slancha(act, installed, markets)
    if keys is not None:
        setup_settings(act, keys)        # last: `claude plugin` writes settings.json too
        for key in SDK_ENV:
            if os.environ.get(key):
                say(f"Warning: {key} is set in your shell. The Langfuse hook reads it before the "
                    "CC_LANGFUSE_* settings, so traces would go elsewhere; remove it from your "
                    "shell profile.")
    if dry_run:
        say(f"Dry run done; run slancha init{' --tracing' if tracing else ''} without --dry-run "
            "to make these changes.")
    elif keys is not None:
        say(f"Done: Langfuse plugin at {LANGFUSE_COMMIT[:8]}, the Slancha plugin, and tracing to "
            f"{keys['host']}.", "Restart Claude Code (quit every running session) to load "
            "them.")
    else:
        say("Done: the Slancha plugin. Restart Claude Code (quit every running session) to load "
            "it.", "Tracing is separate: slancha auth --tracing, then slancha init --tracing.")


# --- uninstall: undo init and auth on this machine ----------------------------------------------

def unmerge_settings(settings: dict, tracing: dict | None) -> list[str]:
    """Take out of Claude Code's settings in place what merge_settings put in; what changed. A
    CC_LANGFUSE_* value stays unless tracing.json is present and holds that same value."""
    notes = []
    env = settings.get("env")
    if isinstance(env, dict):
        ours = {"CC_LANGFUSE_PUBLIC_KEY": "public_key", "CC_LANGFUSE_SECRET_KEY": "secret_key",
                "CC_LANGFUSE_BASE_URL": "host"}
        present = [k for k in ours if k in env]
        # Without tracing.json there is no record of what init wrote, so every key stays.
        removed = [k for k in present if tracing is not None and env[k] == tracing.get(ours[k])]
        for key in removed:
            del env[key]
        if removed:
            notes.append(f"remove env {', '.join(removed)}")
        kept = [k for k in present if k not in removed]
        if kept:
            why = ("no tracing.json, so these are left as they are" if tracing is None else
                   "not the values slancha init wrote")
            notes.append(f"keep env {', '.join(kept)}: {why}")
    configs = settings.get("pluginConfigs")
    entry = configs.get(LANGFUSE_PLUGIN) if isinstance(configs, dict) else None
    options = entry.get("options") if isinstance(entry, dict) else None
    if isinstance(options, dict):
        removed = [k for k, v in LANGFUSE_OPTIONS.items() if k in options and options[k] == v]
        for key in removed:
            del options[key]
        if removed:
            notes.append(f"remove pluginConfigs[\"{LANGFUSE_PLUGIN}\"].options "
                         + ", ".join(removed))
        if not options and set(entry) == {"options"}:
            del configs[LANGFUSE_PLUGIN]
    return notes


def teardown_settings(act: Actions, tracing: dict | None) -> None:
    path = claude_config_dir() / "settings.json"
    real = Path(os.path.realpath(path))
    try:
        before = real.read_text(encoding="utf-8")
        settings = json.loads(before)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        settings = None
    if not isinstance(settings, dict):
        raise SlanchaError(f"{shown(path)} is not a JSON object; fix it, then run slancha "
                           "uninstall again.")
    notes = unmerge_settings(settings, tracing)
    say(f"Claude Code settings ({shown(path)}):")
    if not notes:
        say("  nothing of Slancha's in it")
        return
    for note in notes:
        say(f"  {'would ' if act.dry_run else ''}{note}")
    if not act.dry_run:
        write_private(real, json.dumps(settings, indent=2) + "\n")


def uninstall(dry_run: bool) -> None:
    """Undo slancha init and slancha auth on this machine. Skills it installed stay where they
    are and stop updating; the personal token stays valid until it is revoked in the console."""
    act = Actions(dry_run)
    if dry_run:
        say("Dry run: nothing is changed.")
    tracing = read_json(config_dir() / "tracing.json")
    tracing = tracing if isinstance(tracing, dict) else None
    if shutil.which("claude") is None:
        say("Claude Code is not on PATH: its plugins are left as they are.")
    else:
        installed = {row["id"] for row in json_list(["claude", "plugin", "list", "--json"])
                     if isinstance(row.get("id"), str)}
        markets = {row["name"] for row in json_list(["claude", "plugin", "marketplace", "list",
                                                     "--json"]) if isinstance(row.get("name"), str)}
        say("Claude Code plugins:")
        for plugin in (SLANCHA_PLUGIN, LANGFUSE_PLUGIN):
            if plugin in installed:
                act.run(["claude", "plugin", "uninstall", plugin, "--scope", "user"])
        for market in (SLANCHA_MARKETPLACE, LANGFUSE_MARKETPLACE):
            if market in markets:
                act.run(["claude", "plugin", "marketplace", "remove", market])
    teardown_settings(act, tracing)     # after `claude plugin`, which writes settings.json too
    unsent = len(read_spool())
    share = Path.home() / ".local" / "share" / "slancha"
    command = Path.home() / ".local" / "bin" / "slancha"
    ours = command.is_file() and str(share / "slancha.py") in command.read_text(errors="replace")
    say("Files:")
    for path in [config_dir(), cache_dir(), share] + ([command] if ours else []):
        if not path.exists():
            continue
        say(f"  {'would remove' if dry_run else 'removing'} {shown(path)}")
        if not dry_run:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    if unsent:
        say(f"  {unsent} usage event{'s' if unsent != 1 else ''} not yet sent to Slancha "
            f"{'would be' if dry_run else 'were'} dropped")
    managed = [p.parent.name for p in user_skills_root().glob(f"*/{MANIFEST}")]
    if managed:
        say(f"Skills installed by Slancha stay in {shown(user_skills_root())} and no longer "
            f"update: {', '.join(sorted(managed))}.")
    say("Revoke the personal token in the Slancha console (Claude Code page) if it was this "
        "machine's." if not dry_run else "Dry run done; run slancha uninstall without --dry-run.")


# --- tracing: what this machine's Claude Code sends to the org's Langfuse -------------------------
# Connecting the org's Langfuse project is an admin's job in the console; these commands only read
# and change this machine's side of it. None of them prints the secret key.

SKILL_TAGS_OFF = ("skill tags are off, so Slancha cannot count traces per skill; any slancha "
                  "tracing config change turns them back on")


def read_settings() -> tuple[Path, dict]:
    """Claude Code's user settings.json: its real path (it may be a link) and its object."""
    path = claude_config_dir() / "settings.json"
    real = Path(os.path.realpath(path))
    try:
        settings = json.loads(real.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return real, {}
    except (OSError, ValueError):
        settings = None
    if not isinstance(settings, dict):
        raise SlanchaError(f"{shown(path)} is not a JSON object; fix it first.")
    return real, settings


def langfuse_options(settings: dict) -> dict:
    configs = settings.get("pluginConfigs")
    entry = configs.get(LANGFUSE_PLUGIN) if isinstance(configs, dict) else None
    options = entry.get("options") if isinstance(entry, dict) else None
    return options if isinstance(options, dict) else {}


def masked(options: dict) -> dict:
    return {k: "(set)" if "SECRET" in k.upper() else v for k, v in options.items()}


def langfuse_row() -> dict | None:
    """The Langfuse plugin as `claude plugin list` reports it, or None when not installed."""
    return next((row for row in json_list(["claude", "plugin", "list", "--json"])
                 if row.get("id") == LANGFUSE_PLUGIN), None)


def notes(row: dict | None) -> list[str]:
    """What Claude Code says about the plugin, e.g. that a project setting overrides the user's."""
    found = row.get("notes") if row else None
    return [printable(n) for n in found if isinstance(n, str)] if isinstance(found, list) else []


def tracing_local() -> dict:
    plugin = {"id": LANGFUSE_PLUGIN, "installed": None, "enabled": None, "scope": None,
              "notes": [], "problem": None}
    if shutil.which("claude") is None:
        plugin["problem"] = "`claude` is not on PATH"
    else:
        try:
            row = langfuse_row()
        except SlanchaError as exc:
            plugin["problem"] = str(exc)
        else:
            plugin.update(installed=row is not None, notes=notes(row),
                          enabled=row.get("enabled") if row else None,
                          scope=row.get("scope") if row else None)
    repo = langfuse_dir()
    head = git_head(repo) if (repo / ".git").exists() and shutil.which("git") else ""
    where = {"path": shown(claude_config_dir() / "settings.json"), "host": None,
             "keys_set": False, "options": {}, "problem": None}
    try:
        settings = read_settings()[1]
    except SlanchaError as exc:
        where["problem"] = str(exc)
    else:
        env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
        host = env.get("CC_LANGFUSE_BASE_URL")
        where.update(host=host if isinstance(host, str) else None,
                     keys_set=bool(env.get("CC_LANGFUSE_PUBLIC_KEY")
                                   and env.get("CC_LANGFUSE_SECRET_KEY")),
                     options=masked(langfuse_options(settings)))
    return {"plugin": plugin, "clone": {"path": shown(repo), "pinned": LANGFUSE_COMMIT,
                                        "head": head or None}, "settings": where}


def day(at) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(at)) if isinstance(at, (int, float)) else "?"


def tracing_status(cfg: Config, args):
    local = tracing_local()
    workspace, error = None, None
    try:
        workspace = api_json(cfg, "GET", "/api/tracing/status")
    except SlanchaError as exc:          # the machine's side is worth showing all the same
        error = str(exc)
    plugin, clone, where = local["plugin"], local["clone"], local["settings"]
    lines = ["On this machine:"]
    if plugin["problem"]:
        lines.append(f"  Langfuse plugin: unknown ({plugin['problem']})")
    elif not plugin["installed"]:
        lines.append("  Langfuse plugin: not installed (slancha auth --tracing, then slancha init "
                     "--tracing)")
    else:
        lines.append(f"  Langfuse plugin: {'enabled' if plugin['enabled'] else 'disabled'}, "
                     f"installed for scope {printable(plugin['scope'])}")
    lines += [f"    Claude Code: {n}" for n in plugin["notes"]]
    head = clone["head"]
    lines.append(f"  Clone {clone['path']}: " + (
        f"at the pinned commit {LANGFUSE_COMMIT[:8]}" if head == LANGFUSE_COMMIT else
        f"at {head[:8]}, not the pinned {LANGFUSE_COMMIT[:8]} (slancha init --tracing)" if head
        else "not there"))
    if where["problem"]:
        lines.append(f"  Settings: {where['problem']}")
    else:
        lines.append(f"  Settings {where['path']}: " + (
            f"traces go to {printable(where['host'])}" if where["host"] else "no Langfuse host")
            + (", keys set" if where["keys_set"] else ", keys not set"))
        if where["options"]:
            lines.append("    options: " + ", ".join(f"{k}={json.dumps(v)}"
                                                    for k, v in sorted(where["options"].items())))
        if where["options"].get("CC_LANGFUSE_SKILL_TAGS") is False:
            lines.append(f"    Warning: {SKILL_TAGS_OFF}.")
    lines.append("In your workspace:")
    if error:
        lines.append(f"  {error}")
    elif not isinstance(workspace, dict) or not workspace.get("configured"):
        lines.append("  Tracing is not connected: an admin connects the workspace's Langfuse "
                     "project in the console.")
    else:
        traces = workspace.get("traces")
        lines.append(f"  Langfuse {printable(workspace.get('host'))}: "
                     + (f"{traces} traces since {day(workspace.get('since'))}"
                        if workspace.get("connected") else "connected, no traces yet"))
        counts = workspace.get("skills")
        if isinstance(counts, dict) and counts:
            lines.append("  per skill: " + ", ".join(f"{printable(k, 64)} {v}"
                                                     for k, v in sorted(counts.items())))
        projects = [p for p in workspace.get("projects") or [] if isinstance(p, dict)]
        if projects:                     # the names slancha draft --new --project takes
            lines.append("  projects: " + ", ".join(
                f"{printable(p.get('name'), 128)} ({p.get('sessions')} sessions)"
                for p in projects[:20]))
    return {"local": local, "workspace": workspace, "workspace_error": error}, lines, 0


def tracing_switch(cfg: Config, args):
    """`claude plugin enable|disable` the Langfuse plugin, for the user or (--project) only in
    the current directory's .claude/settings.local.json, the scope that is not committed."""
    on = args.action == "on"
    if shutil.which("claude") is None:
        raise SlanchaError("`claude` is not on PATH: install Claude Code.")
    if langfuse_row() is None:
        if not on:
            return ({"tracing": "off", "changed": False, "installed": False},
                    ["The Langfuse plugin is not installed, so nothing is traced."], 0)
        raise SlanchaError("The Langfuse plugin is not installed. Setting tracing up takes, in "
                           "order: an admin connects the workspace's Langfuse project in the "
                           "console; run slancha auth --tracing, which an org admin approves, "
                           "then slancha init --tracing. slancha tracing status shows what is "
                           "done.")
    scope = "local" if args.project else "user"
    where = (f"in this project ({shown(Path.cwd() / '.claude' / 'settings.local.json')}, not "
             "committed)" if args.project
             else f"for your user ({shown(claude_config_dir() / 'settings.json')})")
    cmd = ["claude", "plugin", "enable" if on else "disable", LANGFUSE_PLUGIN, "--scope", scope,
           "--json"]
    result = run(cmd)
    try:
        answer = json.loads((result.stdout.strip().splitlines() or [""])[-1])
    except ValueError:
        answer = None
    # Claude Code exits 1 when the plugin is already in that state; that is no failure here.
    if result.returncode != 0 and not (isinstance(answer, dict)
                                       and answer.get("alreadyInGoalState") is True):
        raise failed(cmd, result)
    state = "on" if on else "off"
    row = langfuse_row()
    lines = [f"Tracing {'is now' if result.returncode == 0 else 'was already'} {state} {where}."]
    lines += [f"Claude Code: {n}" for n in notes(row)]
    lines.append("New Claude Code sessions follow it; restart the ones running.")
    return ({"tracing": state, "scope": scope, "changed": result.returncode == 0,
             "enabled": row.get("enabled") if row else None, "notes": notes(row)}, lines, 0)


def tracing_config(cfg: Config, args):
    real, settings = read_settings()
    path = shown(claude_config_dir() / "settings.json")
    options = langfuse_options(settings)
    wanted = {}
    if args.max_chars is not None:
        wanted["CC_LANGFUSE_MAX_CHARS"] = args.max_chars
    if args.images is not None:
        wanted["CC_LANGFUSE_CAPTURE_IMAGES"] = args.images == "on"
    if not wanted:
        lines = [f"Langfuse plugin options in {path}:"] + (
            [f"  {k} = {json.dumps(v)}" for k, v in sorted(masked(options).items())]
            or ["  none set: the plugin's defaults apply"])
        if options.get("CC_LANGFUSE_SKILL_TAGS") is False:
            lines.append(f"Warning: {SKILL_TAGS_OFF}.")
        return {"path": path, "options": masked(options), "changed": []}, lines, 0
    # Slancha counts traces per skill by their skill tags: whatever else changes, they stay on.
    wanted["CC_LANGFUSE_SKILL_TAGS"] = True
    changed = {k: v for k, v in wanted.items() if options.get(k) != v}
    if changed:
        configs = settings.setdefault("pluginConfigs", {})
        entry = configs.setdefault(LANGFUSE_PLUGIN, {}) if isinstance(configs, dict) else None
        options = entry.setdefault("options", {}) if isinstance(entry, dict) else None
        if not isinstance(options, dict):
            raise SlanchaError(f"{path} has a `pluginConfigs` that is not an object; fix it first.")
        options.update(changed)
        real.parent.mkdir(parents=True, exist_ok=True)
        write_private(real, json.dumps(settings, indent=2) + "\n")   # it may hold the secret
    shown_set = ", ".join(f"{k}={json.dumps(v)}" for k, v in wanted.items())
    lines = [f"{'Set' if changed else 'Already set'} in {path}: {shown_set}."]
    if "CC_LANGFUSE_SKILL_TAGS" in changed:
        lines.append("Skill tags were off and are on again: Slancha counts traces per skill by "
                     "them.")
    if changed:
        lines.append("Written with mode 0600 (readable only by you). New Claude Code sessions use "
                     "it.")
    return {"path": path, "options": masked(options), "changed": sorted(changed)}, lines, 0


# --- the library, its jobs and criteria, for a person or a coding agent --------------------------
# Each command answers (JSON document, human lines, exit code). Past the plain token's routes they
# need an agent token (slancha auth --manage). None of them approves, rejects or rolls back a
# change, or sets criteria: people do that in the console.

DRAFT_NOTICE = "traces-to-model-provider-v1"
CRITERIA_UNMET = "acceptance criteria not met:"
JOB_ENDS = ("succeeded", "failed", "cancelled")
CRITERIA_FAILED = 3


def api_object(cfg: Config, method: str, path: str, **kwargs) -> dict:
    doc = api_json(cfg, method, path, **kwargs)
    if not isinstance(doc, dict):
        raise SlanchaError(f"Slancha answered {path.split('?')[0]} with something other than an "
                           "object.")
    return doc


def api_rows(cfg: Config, path: str) -> list[dict]:
    rows = api_json(cfg, "GET", path)
    if not isinstance(rows, list):
        raise SlanchaError(f"Slancha answered {path.split('?')[0]} with something other than a "
                           "list.")
    return [row for row in rows if isinstance(row, dict)]


def rev(revision) -> str:
    return short(revision) if isinstance(revision, str) and REVISION.fullmatch(revision) \
        else printable(revision or "-", 64)


def since(at) -> str:
    return f", {ago(at)}" if isinstance(at, (int, float)) and not isinstance(at, bool) else ""


def text_block(text: str) -> list[str]:
    """A file's or a diff's lines as they may reach a terminal: no control characters."""
    return ["".join(c if c.isprintable() or c == "\t" else "?" for c in line)
            for line in text.splitlines()]


def evaluation_lines(evaluation, title: str) -> list[str]:
    """An acceptance-criteria evaluation: a ✓ or ✗ per rule, and why a rule failed."""
    if not isinstance(evaluation, dict):
        return []
    results = [r for r in evaluation.get("results") or [] if isinstance(r, dict)]
    if not results:
        return [f"{title}: no rules to meet"]
    failing = sum(1 for r in results if not r.get("ok"))
    lines = [f"{title}: " + (f"fails {failing} of {len(results)}" if failing
                             else f"meets all {len(results)}")]
    for r in results:
        reason = f": {printable(r.get('reason'))}" if not r.get("ok") and r.get("reason") else ""
        lines.append(f"  {'✓' if r.get('ok') else '✗'} {printable(r.get('label'))}{reason}")
    return lines


def criteria_lines(doc: dict) -> list[str]:
    rules = doc.get("rules") if isinstance(doc.get("rules"), list) else []
    if not rules:
        return ["Acceptance criteria: none (an admin sets them in the console)."]
    lines = [f"Acceptance criteria: {len(rules)} rule{'s' if len(rules) != 1 else ''}"
             + (f", set by {printable(doc['updated_by'], 120)}" if doc.get("updated_by") else "")
             + since(doc.get("updated_at")) + "."]
    evaluated = False
    for key, title in (("served", "Served"), ("pending", "Waiting for review")):
        evaluation = doc.get(key)
        if isinstance(evaluation, dict):
            evaluated = True
            lines += evaluation_lines(evaluation, f"{title} {rev(evaluation.get('revision'))}")
    if not evaluated:            # no revision to show them against: list the rules themselves
        lines += [f"  - {printable(label)}" for label in doc.get("labels") or []
                  if isinstance(label, str)]
    return lines


def gate_lines(gate, held: str | None = None) -> list[str]:
    """The gate as the console's approve button reads it: `held` says why a change the gate
    itself would allow still cannot be approved (a stale proposal, a scan not clean yet)."""
    if not isinstance(gate, dict):
        return []
    lines = ["Gate: " + ("can be approved" if gate.get("promotable") and not held else "blocked")]
    lines += [f"  blocked: {printable(b)}" for b in gate.get("blocked") or []]
    if held:
        lines.append(f"  blocked: {printable(held)}")
    return lines + [f"  warning: {printable(w)}" for w in gate.get("warnings") or []]


def job_line(job: dict) -> str:
    cost = job.get("cost_usd")
    return (f"{printable(job.get('id'), 80)}  {printable(job.get('type'), 20)}  "
            f"{printable(job.get('skill'), 64)}  {rev(job.get('revision'))}  "
            f"{printable(job.get('status'), 20)}"
            + (f"  ${cost:.2f}" if isinstance(cost, (int, float)) and cost else "")
            + since(job.get("created_at")))


def job_lines(job: dict) -> list[str]:
    lines = [f"Job {job_line(job)}"]
    progress = job.get("progress")
    if isinstance(progress, dict) and progress.get("message"):
        lines.append(f"  progress: {printable(progress['message'])}")
    if job.get("error"):
        lines.append(f"  error: {printable(job['error'], 500)}")
    if isinstance(job.get("result"), dict) and job["result"]:
        lines.append("  result: " + printable(json.dumps(job["result"], sort_keys=True), 1000))
    return lines


def wait_job(cfg: Config, job: dict, quiet: bool) -> dict:
    """Poll the job until it ends: 2 s apart at first, then up to 30 s apart."""
    delay, seen = 2.0, None
    while job.get("status") not in JOB_ENDS:
        progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
        now = (job.get("status"), progress.get("message"))
        if not quiet and now != seen:
            say(f"  {printable(now[0], 20)}" + (f": {printable(now[1])}" if now[1] else ""))
        seen = now
        time.sleep(delay)
        delay = min(30.0, delay * 1.5)
        job = api_object(cfg, "GET", f"/api/jobs/{urllib.parse.quote(str(job.get('id')), safe='')}")
    return job


def _zip_path(name: str) -> None:
    """Refuse a zip entry that is absolute or climbs out of the skill folder."""
    text = name[:-1] if name.endswith("/") else name
    if not text or "\\" in text or text.startswith("/") or any(part in ("", ".", "..")
                                                              for part in text.split("/")):
        raise SlanchaError(f"{name!r} leaves the skill's folder; a skill holds no such path.")


def check_zip(path: Path, where: str) -> None:
    """The same limits as a directory upload, read from the zip's central directory."""
    try:
        bundle = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise SlanchaError(f"{where} is not a zip file.") from None
    files, total = 0, 0
    with bundle:
        for info in bundle.infolist():
            name = info.filename
            _zip_path(name)
            if name.endswith("/") or info.is_dir():
                continue
            kind = stat.S_IFMT(info.external_attr >> 16)
            if info.flag_bits & 0x1:
                raise SlanchaError(f"{name} is encrypted; a skill holds plain files only.")
            if kind not in (0, stat.S_IFREG):
                raise SlanchaError(f"{name} is a link; a skill holds plain files only.")
            files += 1
            size = info.file_size
            total += size
            if files > MAX_FILES or size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise SlanchaError(f"{where} is too large at {name}: a skill has at most "
                                   f"{MAX_FILES} files, {MAX_FILE_BYTES} bytes each and "
                                   f"{MAX_TOTAL_BYTES} in all.")
    if files == 0:
        raise SlanchaError(f"{where} holds no files.")


def package(where: str) -> tuple[dict, list[str]]:
    """The upload body for a skill: a directory of text files, or a .zip as it is. Hidden files,
    __pycache__ and an installed copy's slancha-manifest.json are left out of a directory (the
    server refuses them). A zip is sent unchanged, after the same limits: 200 files, 256 KB each,
    2 MB in all, no links, and no path that leaves the skill's folder."""
    path = Path(where).expanduser()
    if path.is_file():
        if path.suffix.lower() != ".zip":
            raise SlanchaError(f"{where} is a file but not a .zip; upload a skill's directory or "
                               "a zip of it.")
        if path.stat().st_size > MAX_ZIP_BYTES:
            raise SlanchaError(f"{where} is larger than {MAX_ZIP_BYTES} bytes, the most Slancha "
                               "takes.")
        check_zip(path, where)
        return {"archive": base64.b64encode(path.read_bytes()).decode("ascii")}, []
    if not path.is_dir():
        raise SlanchaError(f"{where} is neither a directory nor a .zip file.")
    files, left_out, total = [], [], 0
    for dirpath, dirnames, filenames in os.walk(path):
        here = Path(dirpath)
        for name in sorted(dirnames):
            rel = (here / name).relative_to(path).as_posix()
            if (here / name).is_symlink():
                raise SlanchaError(f"{rel} is a link; a skill holds plain files only.")
            if name.startswith(".") or name == "__pycache__":
                dirnames.remove(name)
                left_out.append(rel + "/")
        dirnames.sort()
        for name in sorted(filenames):
            full = here / name
            rel = full.relative_to(path).as_posix()
            if name.startswith(".") or rel == MANIFEST:
                left_out.append(rel)
                continue
            if full.is_symlink() or not full.is_file():
                raise SlanchaError(f"{rel} is a link; a skill holds plain files only.")
            size = full.stat().st_size
            total += size
            if len(files) == MAX_FILES or size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise SlanchaError(f"{where} is too large at {rel}: a skill has at most "
                                   f"{MAX_FILES} files, {MAX_FILE_BYTES} bytes each and "
                                   f"{MAX_TOTAL_BYTES} in all.")
            try:
                files.append({"path": rel, "content": full.read_bytes().decode("utf-8")})
            except UnicodeDecodeError:
                raise SlanchaError(f"{rel} is not UTF-8 text; a skill holds text files only.") \
                    from None
    if not files:
        raise SlanchaError(f"{where} holds no files.")
    return {"files": files}, left_out


def left_out_line(left_out: list[str], as_json: bool) -> list[str]:
    if not left_out:
        return []
    line = "Left out: " + ", ".join(left_out[:10]) + (f" and {len(left_out) - 10} more"
                                                      if len(left_out) > 10 else "")
    if as_json:
        print(line, file=sys.stderr)       # stdout holds only the JSON
        return []
    return [line]


def whoami(cfg: Config, args):
    me = api_object(cfg, "GET", "/api/me")
    org = me.get("org") if isinstance(me.get("org"), dict) else {}
    caps = me.get("capabilities") if isinstance(me.get("capabilities"), list) else []
    lines = [f"{printable(me.get('email') or me.get('user') or '?', 120)}: "
             f"{printable(me.get('role'), 40)} in the workspace "
             f"{printable(org.get('name') or org.get('id') or '?', 120)}",
             token_line(caps, me.get("expires_at"))]
    if "manage" not in caps:
        lines.append("This token reads published skills and reports their use, nothing more; "
                     "slancha auth --manage signs in an agent token.")
    else:
        allowed = api_object(cfg, "GET", "/api/agent-jobs").get("enabled") is True
        lines.append("As an agent token it reads skills, reviews and jobs, checks acceptance "
                     "criteria, uploads and scans. Drafts and outcome jobs: "
                     + ("allowed; an admin turned them on." if allowed else
                        "not allowed until an admin turns them on in Workspace settings.")
                     + " Approving, rejecting, rolling back and setting criteria are for people "
                     "in the console.")
    return me, lines, 0


def skills(cfg: Config, args):
    rows = api_rows(cfg, "/api/skills")
    lines = []
    for row in rows:
        state = f"serves {rev(row.get('revision'))}" if row.get("active") else "not served"
        if row.get("pending"):
            state += f", {printable(row.get('pending_kind') or 'a change', 20)} waiting for review"
        lines.append(f"  {printable(row.get('name'), 64)}  {state}  {row.get('uses_7d', 0)} uses "
                     "in 7 days" + (f"  {printable(row['description'], 80)}"
                                    if row.get("description") else ""))
    return rows, lines or ["Your Slancha library has no skills yet."], 0


def skill(cfg: Config, args):
    name = args.skill
    versions = api_object(cfg, "GET", f"/api/skills/{name}/versions")
    criteria = api_object(cfg, "GET", f"/api/skills/{name}/criteria")
    lines = [f"{name}: " + (f"serves {rev(versions['active'])}" if versions.get("active")
                            else "not served")]
    pending = versions.get("pending")
    if isinstance(pending, dict):
        lines.append(f"Waiting for review: {printable(pending.get('kind'), 20)} "
                     f"{rev(pending.get('revision'))}, proposal "
                     f"{printable(pending.get('id'), 80)}{since(pending.get('created'))}; "
                     f"slancha pending {name} shows whether it can be approved")
    publications = [p for p in versions.get("publications") or [] if isinstance(p, dict)]
    if publications:
        lines.append("Publications, newest first:")
        lines += [f"  {day(p.get('at'))}  {printable(p.get('action'), 20)}  "
                  f"{rev(p.get('from_revision'))} -> {rev(p.get('to_revision'))}  by "
                  f"{printable(p.get('actor'), 120)}" for p in publications[:10]]
        if len(publications) > 10:
            lines.append(f"  and {len(publications) - 10} more (--json lists them all)")
    return {"versions": versions, "criteria": criteria}, lines + criteria_lines(criteria), 0


def name_in_text(text: str) -> str | None:
    """The frontmatter `name` of a SKILL.md, when it is a skill name."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:60]:
        if line.strip() == "---":
            break
        found = re.match(r"name:\s*['\"]?([^'\"#]+?)['\"]?\s*$", line)
        if found and valid_name(found.group(1).strip()):
            return found.group(1).strip()
    return None


def zip_identity(path: Path) -> tuple[str | None, str | None]:
    """(single top folder, SKILL.md frontmatter name) of a zip, either of which may be missing.
    The upload endpoint uses `--name`, else that top folder, else the frontmatter name."""
    try:
        with zipfile.ZipFile(path) as bundle:
            names = [n for n in bundle.namelist() if n and not n.endswith("/")]
            tops = {n.split("/", 1)[0] for n in names}
            folder = next(iter(tops)) if len(tops) == 1 and all("/" in n for n in names) else None
            if folder and not valid_name(folder):
                folder = None
            chosen = sorted((n for n in names if n == "SKILL.md" or n.endswith("/SKILL.md")),
                            key=lambda n: (n.count("/"), len(n)))
            text = bundle.read(chosen[0]).decode("utf-8") if chosen else ""
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, KeyError):
        return None, None
    return folder, name_in_text(text) if text else None


def implied_name(where: str, body: dict) -> str | None:
    """The name an upload would be stored under: a directory's SKILL.md, or a zip's single top
    folder and, when it has none, its SKILL.md. That is the order POST /api/skills/upload uses."""
    if "files" in body:
        text = next((f["content"] for f in body["files"]
                     if isinstance(f, dict) and f.get("path") == "SKILL.md"
                     and isinstance(f.get("content"), str)), None)
        return name_in_text(text) if text else None
    folder, front = zip_identity(Path(where).expanduser())
    return folder or front


def pending_message(name: str | None) -> str:
    """What to say when a skill already has a change waiting: the next upload is refused."""
    if name:
        return (f"{name}: a change is already waiting for review, so it takes no other upload "
                f"until that change is approved or rejected. slancha pending {name} shows it.")
    return ("A change is already waiting for review, so this skill takes no other upload until "
            "that change is approved or rejected. slancha pending <skill> shows it.")


def _pending_conflict(exc: ApiError) -> bool:
    return (exc.status == 409 and "waiting for review" in exc.detail.lower()
            and not exc.detail.startswith(CRITERIA_UNMET))


class _Pending:
    """Skills that already have a change waiting, fetched once and extended as this run stores."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.known: set[str] | None = None
        self.extra: set[str] = set()
        self.unreachable = False

    def waiting(self, name: str) -> bool:
        if name in self.extra:
            return True
        if self.known is None:
            try:
                rows = api_rows(self.cfg, "/api/pending")
            except SlanchaError:
                self.known, self.unreachable = set(), True
            else:
                self.known = {row["skill"] for row in rows if isinstance(row.get("skill"), str)}
        return False if self.unreachable else name in (self.known or set())

    def mark(self, name: str) -> None:
        self.extra.add(name)


@dataclass
class _Target:
    path: str
    error: str | None = None
    collected: bool = False


@dataclass
class _Outcome:
    path: str
    skill: str | None
    code: int
    lines: list[str]
    doc: dict | None = None
    error: str | None = None
    status: int | None = None
    uploaded: bool = False
    left_out: list[str] | None = None
    dry_run: bool = False
    pending: bool = False


def _collection(path: Path) -> tuple[list[Path], list[str]]:
    """Child skill directories and zips, and the names of everything else that was skipped."""
    skills, skipped = [], []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        if child.name.startswith("."):
            continue
        if child.is_dir() and (child / "SKILL.md").is_file():
            skills.append(child)
        elif child.is_file() and child.suffix.lower() == ".zip":
            skills.append(child)
        else:
            skipped.append(child.name + ("/" if child.is_dir() else ""))
    return skills, skipped


def resolve_uploads(paths: list[str]) -> tuple[list[_Target], list[str]]:
    """Each path is one skill, unless it is a directory with no SKILL.md: then it is a folder of
    skill directories and zips, one skill each. Hidden children are ignored."""
    targets, notes = [], []
    for raw in paths:
        path = Path(raw).expanduser()
        if not (path.is_dir() and not (path / "SKILL.md").is_file()):
            targets.append(_Target(raw))
            continue
        try:
            children, skipped = _collection(path)
        except OSError as exc:
            targets.append(_Target(raw, error=f"{raw}: cannot read it ({exc.strerror or exc}).",
                                   collected=True))
            continue
        if skipped:
            shown = ", ".join(skipped[:10])
            if len(skipped) > 10:
                shown += f" and {len(skipped) - 10} more"
            notes.append(f"Skipped in {raw} (not a skill folder or .zip): {shown}.")
        if not children:
            targets.append(_Target(raw, error=(
                f"{raw} has no skill to upload. Pass a skill directory (SKILL.md at its top) or a "
                ".zip of one, or a folder whose children are those."), collected=True))
        else:
            targets.extend(_Target(str(child), collected=True) for child in children)
    return targets, notes


def _upload_lines(doc: dict, left_out: list[str], as_json: bool) -> tuple[list[str], int]:
    criteria = doc.get("criteria")
    unmet = isinstance(criteria, dict) and criteria.get("ok") is False
    gate = doc.get("gate")
    if unmet and isinstance(gate, dict):           # as the API does, should it not have
        gate = {**gate, "promotable": False}
    lines = left_out_line(left_out, as_json) + [
        f"Uploaded {printable(doc.get('skill'), 64)}: revision {rev(doc.get('revision'))}, "
        f"proposal {printable(doc.get('proposal_id'), 80)} ({printable(doc.get('kind'), 20)}). "
        "It waits for a person to review it in the console."]
    scan_error = doc.get("scan_error")
    lines += gate_lines(gate, held="its security scan did not start" if scan_error else
                        "its security scan has not come back clean yet"
                        ) + evaluation_lines(criteria, "Acceptance criteria")
    if scan_error:
        lines.append(f"Security scan: {printable(scan_error)}. The change is stored and waits for "
                     "review, so do not upload it again: slancha scan "
                     f"{printable(doc.get('skill'), 64)} --wait starts the scan again.")
    elif doc.get("scan_job"):
        lines.append(f"Security scan: job {printable(doc['scan_job'], 80)}; slancha job "
                     f"{printable(doc['scan_job'], 80)} --wait follows it.")
    if unmet:
        lines.append("The change is stored but cannot be approved until it meets the acceptance "
                     "criteria, and no other upload of the skill is taken until a person rejects "
                     "it. Check a package first: slancha criteria <skill> --check <path>.")
    return lines, CRITERIA_FAILED if unmet else 1 if scan_error else 0


def propose(cfg: Config, where: str, name: str | None, as_json: bool,
            waiting: _Pending) -> tuple[dict, list[str], int, list[str], str | None]:
    """Upload one skill. The name is `--name` or the one the package implies."""
    body, left_out = package(where)
    if name:
        body["name"] = name
    skill = name or implied_name(where, body)
    if skill and waiting.waiting(skill):
        raise ApiError(409, pending_message(skill), "a change is already waiting for review")
    try:
        doc = api_object(cfg, "POST", "/api/skills/upload", body=body, timeout=120)
    except ApiError as exc:
        if _pending_conflict(exc):
            raise ApiError(409, pending_message(skill), exc.detail) from None
        raise
    lines, code = _upload_lines(doc, left_out, as_json)
    stored = doc.get("skill") if isinstance(doc.get("skill"), str) else skill
    return doc, lines, code, left_out, stored if isinstance(stored, str) else None


def dry_run_one(cfg: Config, where: str, name: str | None, waiting: _Pending) -> _Outcome:
    """Package and validate locally, then check acceptance criteria. Nothing is uploaded."""
    body, left_out = package(where)
    skill = name or implied_name(where, body)
    lines = left_out_line(left_out, False)
    pending = bool(skill and waiting.waiting(skill))
    if pending:
        lines.append(pending_message(skill) + " An upload would be refused.")
    if not skill:
        message = ("Could not read a skill name from SKILL.md, so the acceptance criteria were "
                   "not checked. Name it in the frontmatter, or pass --name with one skill.")
        lines.append(message)
        return _Outcome(where, None, 1, lines, error=message, left_out=left_out, dry_run=True,
                        pending=pending)
    try:
        doc = api_object(cfg, "POST", f"/api/skills/{skill}/criteria/check", body=body, timeout=60)
    except ApiError as exc:
        code = CRITERIA_FAILED if exc.status == 409 and exc.detail.startswith(CRITERIA_UNMET) \
            else 1
        lines.append(str(exc))
        return _Outcome(where, skill, code, lines, error=str(exc), status=exc.status,
                        left_out=left_out, dry_run=True, pending=pending)
    lines += evaluation_lines(doc, f"{where} against the acceptance criteria of {skill}")
    unmet = isinstance(doc, dict) and doc.get("ok") is False
    code = CRITERIA_FAILED if unmet else 1 if pending else 0
    return _Outcome(where, skill, code, lines, doc=doc, left_out=left_out, dry_run=True,
                    pending=pending)


def _outcome_json(row: _Outcome) -> dict:
    doc = {"path": row.path, "skill": row.skill, "exit": row.code, "dry_run": row.dry_run,
           "uploaded": row.uploaded}
    if row.left_out:
        doc["left_out"] = row.left_out
    if row.pending:
        doc["pending"] = True
    if row.error:
        doc["error"] = row.error
        doc["status"] = row.status
    if row.doc is not None:
        doc["criteria" if row.dry_run else "result"] = row.doc
    return doc


def _worst(rows: list[_Outcome]) -> int:
    """3 when any skill misses its acceptance criteria, else 1 when any failed, else 0."""
    if any(row.code == CRITERIA_FAILED for row in rows):
        return CRITERIA_FAILED
    if any(row.code != 0 for row in rows):
        return 1
    return 0


def _batch_lines(rows: list[_Outcome], notes: list[str], dry_run: bool) -> list[str]:
    lines = ["Dry run: nothing is uploaded."] if dry_run else []
    lines += notes
    for row in rows:
        lines.append(f"{row.skill or row.path} ({row.path}):")
        lines += [f"  {line}" if line else "" for line in row.lines]
    ok = sum(row.code == 0 for row in rows)
    missed = sum(row.code == CRITERIA_FAILED for row in rows)
    failed = sum(row.code not in (0, CRITERIA_FAILED) for row in rows)
    count = len(rows)
    lines.append(f"{count} skill{'s' if count != 1 else ''}: {ok} ok, "
                 f"{missed} failed acceptance criteria, {failed} failed."
                 + (" Nothing was uploaded." if dry_run else ""))
    return lines


def _caught(where: str, exc: SlanchaError) -> _Outcome:
    status = exc.status if isinstance(exc, ApiError) else None
    code = CRITERIA_FAILED if isinstance(exc, ApiError) and exc.status == 409 \
        and exc.detail.startswith(CRITERIA_UNMET) else 1
    return _Outcome(where, None, code, [str(exc)], error=str(exc), status=status)


def upload(cfg: Config, args):
    targets, notes = resolve_uploads(args.paths)
    ready = [target for target in targets if target.error is None]
    if args.name and len(ready) > 1:
        raise UsageError("--name names one skill; "
                         f"{len(ready)} were given. Pass a single skill, or omit --name and let "
                         "each SKILL.md name it.")
    waiting = _Pending(cfg)
    one = (not args.dry_run and len(targets) == 1 and targets[0].error is None
           and not targets[0].collected)
    if one:
        doc, lines, code, _, _ = propose(cfg, targets[0].path, args.name, args.json, waiting)
        return doc, lines, code
    rows: list[_Outcome] = []
    for target in targets:
        if target.error:
            rows.append(_Outcome(target.path, None, 1, [target.error], error=target.error))
            continue
        try:
            if args.dry_run:
                rows.append(dry_run_one(cfg, target.path, args.name, waiting))
            else:
                doc, lines, code, left_out, skill = propose(cfg, target.path, args.name, False,
                                                           waiting)
                uploaded = isinstance(doc, dict) and bool(doc.get("proposal_id"))
                if uploaded and skill:
                    waiting.mark(skill)
                rows.append(_Outcome(target.path, skill, code, lines, doc=doc, left_out=left_out,
                                     uploaded=uploaded))
        except SlanchaError as exc:
            rows.append(_caught(target.path, exc))
    if args.json:
        for row in rows:
            left_out_line(row.left_out or [], True)
        for note in notes:
            print(note, file=sys.stderr)
        notes = []
    as_array = any(target.collected for target in targets) or len(rows) != 1
    payload = [_outcome_json(row) for row in rows]
    return (payload if as_array else payload[0]), _batch_lines(rows, notes, args.dry_run), \
        _worst(rows)


def review_card(cfg: Config, name: str) -> dict:
    # comparison_paging: a card too large to carry its diffs is answered, not refused
    return api_object(cfg, "GET", f"/api/pending/{name}?comparison_paging=true")


def card_lines(card: dict) -> list[str]:
    lines = [f"{printable(card.get('skill'), 64)}: {printable(card.get('kind'), 20)} "
             f"{rev(card.get('base_revision'))} -> {rev(card.get('revision'))}, proposal "
             f"{printable(card.get('id'), 80)}, from {printable(card.get('source'), 40)} by "
             f"{printable(card.get('created_by'), 120)}{since(card.get('created'))}"]
    if card.get("stale"):
        lines.append(f"Stale: {printable(card['stale'])}")
    lines += gate_lines(card.get("gate"), held=card.get("stale") or card.get("scan_blocker")
                        ) + evaluation_lines(card.get("criteria"), "Acceptance criteria")
    scan = card.get("scan")
    if isinstance(scan, dict):
        lines.append("Security scan: " + ("clean" if scan.get("ok") else "not clean")
                     + (f", {printable(scan['recommendation'], 200)}"
                        if scan.get("recommendation") else ""))
    if card.get("scan_blocker"):
        lines.append(f"Security scan: {printable(card['scan_blocker'])}")
    risk = card.get("risk")
    if isinstance(risk, dict):
        lines.append(f"SKILL.md body: +{risk.get('added_lines')} -{risk.get('removed_lines')} "
                     f"lines, {risk.get('changed_pct')}% changed"
                     + (" (high risk)" if risk.get("high_risk") else ""))
    files = [f"{printable(f.get('path'), 200)} {printable(f.get('status'), 20)}"
             for f in card.get("files") or [] if isinstance(f, dict)
             and f.get("status") != "unchanged"]
    return lines + (["Files: " + ", ".join(files)] if files else [])


def pending(cfg: Config, args):
    if args.skill is not None:
        card = review_card(cfg, args.skill)
        return card, card_lines(card), 0
    rows = api_rows(cfg, "/api/pending")
    lines = [f"  {printable(r.get('skill'), 64)}  {printable(r.get('kind'), 20)}  "
             f"{rev(r.get('base_revision'))} -> {rev(r.get('revision'))}  proposal "
             f"{printable(r.get('id'), 80)}  by {printable(r.get('created_by'), 120)}"
             f"{since(r.get('created_at'))}" for r in rows]
    return rows, lines or ["Nothing is waiting for review."], 0


def comparison(cfg: Config, name: str, proposal_id: str, component: str) -> str:
    """One component's whole diff, a page (at most 32 K characters) at a time."""
    parts, offset = [], 0
    while True:
        query = urllib.parse.urlencode({"proposal_id": proposal_id, "component": component,
                                        "view": "diff", "offset": offset})
        page = api_object(cfg, "GET", f"/api/pending/{name}/comparison?{query}")
        parts.append(page["content"] if isinstance(page.get("content"), str) else "")
        following = page.get("next_offset")
        if not isinstance(following, int) or following <= offset:
            return "".join(parts)
        offset = following


def review(cfg: Config, args):
    """The review card, the evidence and every changed component's diff; read-only."""
    card = review_card(cfg, args.skill)
    evidence = api_object(cfg, "GET", f"/api/evidence/{args.skill}")
    components = [c for c in card.get("comparison_components") or []
                  if isinstance(c, dict) and isinstance(c.get("id"), str)]
    diffs = {c["id"]: comparison(cfg, args.skill, str(card.get("id")), c["id"])
             for c in components}
    lines = card_lines(card)
    if evidence.get("evidence") is not None:
        lines += ["Evidence:"] + ["  " + line for line in text_block(
            json.dumps(evidence["evidence"], indent=2, sort_keys=True))]
    for c in components:
        lines += ["", f"== {printable(c.get('label') or c['id'], 200)}"]
        lines += text_block(diffs[c["id"]]) or ["(no difference in the text)"]
    lines += ["", "This is read-only: a person approves or rejects the change in the Slancha "
                  "console."]
    return {"card": card, "evidence": evidence, "diffs": diffs}, lines, 0


def _newest_first(row: dict):
    """Sort key: a created_at, newest first; rows without one keep their order at the end."""
    at = row.get("created_at")
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        return (0, -float(at))
    return (1, 0.0)


def jobs(cfg: Config, args):
    query = "?" + urllib.parse.urlencode({"skill": args.skill}) if args.skill else ""
    rows = sorted(api_rows(cfg, f"/api/jobs{query}"), key=_newest_first)
    return rows, [f"  {job_line(j)}" for j in rows] or ["No jobs yet."], 0


def job(cfg: Config, args):
    path = f"/api/jobs/{urllib.parse.quote(args.id, safe='')}"
    found = api_object(cfg, "POST" if args.cancel else "GET", path + ("/cancel" if args.cancel
                                                                      else ""))
    if args.wait:
        found = wait_job(cfg, found, args.json)
    return found, job_lines(found), 1 if args.wait and found.get("status") != "succeeded" else 0


def scan(cfg: Config, args):
    started = api_object(cfg, "POST", f"/api/skills/{args.skill}/scan",
                         body={"revision": args.revision} if args.revision else {})
    if not args.wait:
        return started, job_lines(started) + [
            f"slancha job {printable(started.get('id'), 80)} --wait follows it."], 0
    done = wait_job(cfg, started, args.json)
    return done, job_lines(done), 0 if done.get("status") == "succeeded" else 1


def draft(cfg: Config, args):
    body = {"acknowledge": DRAFT_NOTICE}       # the admin's workspace opt-in is the consent
    if args.new:
        # api_rows reports a missing token; fetch_library would call the API with none.
        rows = {row["name"]: row for row in api_rows(cfg, "/api/skills")
                if isinstance(row.get("name"), str)}
        if published(rows.get(args.skill)):
            raise SlanchaError(f"{args.skill} is already served; without --new, draft improves "
                               "it from the sessions that used it.")
        body["project"] = args.project
        if args.description:
            body["description"] = args.description
    started = api_object(cfg, "POST", f"/api/draft/{args.skill}", body=body, timeout=60)
    return started, job_lines(started) + [
        f"slancha job {printable(started.get('id'), 80)} --wait follows it; the draft becomes a "
        "change waiting for review."], 0


def outcomes(cfg: Config, args):
    if args.start:
        started = api_object(cfg, "POST", f"/api/skills/{args.skill}/outcomes")
        return started, job_lines(started) + [
            f"When it ends, slancha outcomes {args.skill} counts them."], 0
    doc = api_object(cfg, "GET", f"/api/skills/{args.skill}/outcomes?days={args.days}")
    unattributed = doc.get("unattributed")
    lines = [f"{args.skill}: {doc.get('sessions')} sessions since {printable(doc.get('since'), 20)}"
             + (f", {unattributed} of them run by no known revision" if unattributed else "")]
    for row in doc.get("revisions") or []:
        if not isinstance(row, dict):
            continue
        counts = row.get("counts") if isinstance(row.get("counts"), dict) else {}
        rates = row.get("rates") if isinstance(row.get("rates"), dict) else {}
        lines.append(f"  {rev(row.get('revision'))}  {row.get('sessions')} sessions, last "
                     f"{printable(row.get('last_day'), 20)}: " + ", ".join(
                         f"{printable(k, 40)} {v}"
                         + (f" ({rates[k]:.0%})" if isinstance(rates.get(k), (int, float)) else "")
                         for k, v in counts.items()))
    if doc.get("attribution"):
        lines.append(f"Attribution: {printable(doc['attribution'])}")
    return doc, lines, 0


def criteria(cfg: Config, args):
    name = args.skill
    if args.check is None and args.revision is None:
        doc = api_object(cfg, "GET", f"/api/skills/{name}/criteria")
        return doc, [f"{name}:"] + criteria_lines(doc), 0
    body, left_out = ({"revision": args.revision}, []) if args.revision else package(args.check)
    doc = api_object(cfg, "POST", f"/api/skills/{name}/criteria/check", body=body, timeout=60)
    what = f"Revision {rev(args.revision)}" if args.revision else args.check
    lines = left_out_line(left_out, args.json) + evaluation_lines(
        doc, f"{what} against the acceptance criteria of {name}")
    return doc, lines, CRITERIA_FAILED if doc.get("ok") is False else 0


# --- the command line from a terminal ------------------------------------------------------------

EPILOG = """\
Also: slancha update [skill] [--dry-run] [--force] and slancha status, which /slancha:update
and /slancha:status run.

Exit codes: 0 done, 1 an error or a refusal, 2 a usage error, 3 the acceptance criteria are not
met, 130 stopped with Ctrl-C. An upload that exits 1 or 3 after its Uploaded line is stored:
do not upload it again. Several skills exit 3 if any miss the criteria, else 1 if any failed.
--json prints the API's JSON (or the documented wrapper) on stdout; with more than one skill,
or a folder of them, upload prints an array of per-skill results. An error prints
{"error": "<message>", "status": <HTTP status or null>}.

Approving, rejecting and rolling back changes, and setting acceptance criteria, are for people
in the Slancha console; no command here does them."""


def positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"{value!r} is not a positive whole number")
    return number


def days_arg(value: str) -> int:
    if positive(value) > 90:
        raise argparse.ArgumentTypeError(f"{value!r} is more than 90 days")
    return int(value)


def skill_arg(value: str) -> str:
    if not valid_name(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a skill name (lowercase letters, "
                                         "digits and single hyphens)")
    return value


def revision_arg(value: str) -> str:
    if not REVISION.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a revision (64 hex characters)")
    return value


def cli(argv: list[str]) -> int:
    """The terminal commands: exit 1 on a problem, 2 on bad usage, 130 on Ctrl-C."""
    parser = argparse.ArgumentParser(prog="slancha", epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     description="Slancha on this machine.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    au = commands.add_parser("auth", help="sign in; you approve the code in the console",
                             formatter_class=argparse.RawDescriptionHelpFormatter,
                             description="Sign in; you approve the code in the console.\n"
                                         "\n"
                                         "The API is --api, else SLANCHA_API_URL, else "
                                         "~/.config/slancha/config.json, else:\n"
                                         f"{DEFAULT_API_URL}")
    au.add_argument("--api", help="the Slancha API (default: SLANCHA_API_URL, then config.json, "
                                  "then the console API above)")
    au.add_argument("--no-browser", action="store_true", help="only print the approval link")
    au.add_argument("--tracing", action="store_true",
                    help="also ask for the org's tracing keys, for slancha init --tracing; an org "
                         "admin approves")
    au.add_argument("--manage", action="store_true",
                    help="sign in an agent token instead: it also uploads, checks, scans and "
                         "drafts, at most as a proposer, and expires after 30 days")
    ini = commands.add_parser("init", help="configure Claude Code for Slancha")
    ini.add_argument("--dry-run", action="store_true", help="say what would change; change nothing")
    ini.add_argument("--check", action="store_true",
                     help="only check that this machine has what init needs; change nothing")
    ini.add_argument("--tracing", action="store_true",
                     help="also trace every Claude Code session to the org's Langfuse: Langfuse's "
                          "plugin and the keys from slancha auth --tracing (needs uv)")
    un = commands.add_parser("uninstall", help="undo slancha init and slancha auth on this machine")
    un.add_argument("--dry-run", action="store_true", help="say what would change; change nothing")

    tr = commands.add_parser("tracing", help="this machine's tracing to the org's Langfuse")
    tracing = tr.add_subparsers(dest="action", required=True, metavar="action")
    sub = tracing.add_parser("status", help="the Langfuse plugin, its settings, and what the "
                                            "workspace receives (needs an agent token)")
    sub.set_defaults(run=tracing_status)
    for action, verb in (("on", "enable"), ("off", "disable")):
        sub = tracing.add_parser(action, help=f"{verb} the Langfuse plugin in Claude Code")
        sub.add_argument("--project", action="store_true",
                         help="only in the project in the current directory, in its "
                              ".claude/settings.local.json, which is not committed")
        sub.set_defaults(run=tracing_switch)
    sub = tracing.add_parser("config", help="show or set the Langfuse plugin's options",
                             description="Without options, shows them. Skill tags stay on: "
                                         "Slancha counts traces per skill by them.")
    sub.add_argument("--max-chars", type=positive, metavar="N",
                     help="cut each captured input or output at N characters")
    sub.add_argument("--images", choices=("on", "off"), help="upload images to Langfuse")
    sub.set_defaults(run=tracing_config)
    for sub in tracing.choices.values():
        sub.add_argument("--json", action="store_true", help="print JSON")

    def command(name: str, run, summary: str, description: str | None = None, *, named=True):
        sub = commands.add_parser(name, help=summary, description=description or summary)
        if named:
            sub.add_argument("skill", type=skill_arg, help="the skill's name")
        sub.add_argument("--json", action="store_true", help="print the API's JSON")
        sub.set_defaults(run=run)
        return sub

    command("whoami", whoami, "who the token signs in as, its role, capabilities and expiry",
            named=False)
    command("skills", skills, "the skills in your library", named=False)
    command("skill", skill, "a skill's served revision, publications and acceptance criteria")
    sub = command("upload", upload, "propose one or more skills for review",
                  "Uploads each skill as its own change waiting for review, and starts its "
                  "security scan. Pass skill directories (SKILL.md at the top), .zip files, or "
                  "both. A directory with no SKILL.md is a folder of skills: each child directory "
                  "that has SKILL.md, and each .zip, is one skill (hidden children are ignored; "
                  "other children are skipped and named). One failure does not stop the rest.\n\n"
                  "The command exits 3 if any skill misses its acceptance criteria, 1 if any other "
                  "skill fails, and 0 only when every skill succeeds. --name names a single skill "
                  "and is refused when more than one skill is given. --dry-run packages and "
                  "validates each skill locally (200 files, 256 KB each, 2 MB, no links, no path "
                  "that leaves the folder) and checks acceptance criteria the way "
                  "`slancha criteria <skill> --check` does; it uploads nothing.\n\n"
                  "A skill that already has a change waiting takes no other upload until that "
                  "change is approved or rejected. With one skill path, --json prints the API's "
                  "object. With more than one skill, or a folder of them, --json prints an array "
                  "of per-skill results (path, skill, exit, uploaded, and result or error). "
                  "A --dry-run of one skill prints one object with dry_run and criteria.\n\n"
                  "Exit 3 when a stored skill misses its acceptance criteria: it cannot be "
                  "approved. Exit 1 after the Uploaded line when its scan did not start: it is "
                  "stored, so do not upload it again; slancha scan <skill> --wait starts the scan.",
                  named=False)
    sub.add_argument("paths", nargs="+", metavar="path",
                     help="a skill directory or .zip, or a folder of them; repeat for several")
    sub.add_argument("--name", type=skill_arg,
                     help="the skill's name, only when exactly one skill is given "
                          "(default: a directory's SKILL.md name; a zip's single top folder, "
                          "else its SKILL.md name)")
    sub.add_argument("--dry-run", action="store_true",
                     help="package and validate each skill and check its acceptance criteria; "
                          "upload nothing")
    sub = command("pending", pending, "the changes waiting for review, or one skill's review card",
                  named=False)
    sub.add_argument("skill", nargs="?", type=skill_arg, help="show this skill's review card")
    command("review", review, "read a waiting change: its card, evidence and every diff",
            "Shows the review card, the evidence and the diff of every changed component. It is "
            "read-only: people approve or reject changes in the Slancha console.")
    sub = command("jobs", jobs, "recent jobs: scans, drafts and outcomes", named=False)
    sub.add_argument("--skill", type=skill_arg, help="only this skill's")
    sub = command("job", job, "one job; --wait follows it to its end", named=False)
    sub.add_argument("id", help="the job's id, as slancha jobs lists it")
    sub.add_argument("--wait", action="store_true",
                     help="poll until it ends; exit 1 unless it succeeded")
    sub.add_argument("--cancel", action="store_true", help="cancel it first")
    sub = command("scan", scan, "start a security scan of a stored revision",
                  "Scans the revision waiting for review, else the served one, or --revision.")
    sub.add_argument("--revision", type=revision_arg,
                     help="a stored revision: all 64 hex characters, as --json shows it")
    sub.add_argument("--wait", action="store_true",
                     help="poll until it ends; exit 1 unless it succeeded")
    sub = command("draft", draft, "draft a skill from your workspace's traced sessions",
                  "Starts a draft job: scrubbed excerpts of the workspace's traced Claude Code "
                  "sessions go to a model on the workspace's own OpenAI key, which proposes a "
                  "change for a person to review. An admin must first let agent tokens start "
                  "drafts and outcome jobs (Workspace settings in the console); that opt-in is the "
                  "consent "
                  f"this command sends as acknowledge: {DRAFT_NOTICE}. An agent asks the user "
                  "before running it. Without --new it improves a served skill with no change "
                  "waiting, from the sessions that used it.")
    sub.add_argument("--new", action="store_true", help="write a new skill (needs --project)")
    sub.add_argument("--project", metavar="P",
                     help="with --new: the project directory whose sessions it is written from "
                          "(slancha tracing status lists them)")
    sub.add_argument("--description", help="with --new: the skill's one-line description")
    sub = command("outcomes", outcomes, "how the sessions that ran each revision ended")
    sub.add_argument("--start", action="store_true",
                     help="start an outcomes job, which labels new sessions from their traces")
    sub.add_argument("--days", type=days_arg, default=30, help="look back 1 to 90 days "
                                                               "(default 30)")
    sub = command("criteria", criteria, "a skill's acceptance criteria, or check a package "
                                        "against them",
                  "Without options, shows the rules and how the served and waiting revisions fare. "
                  "--check PATH or --revision R checks without storing anything; exit 3 when it "
                  "fails. Admins set the rules in the console.")
    which = sub.add_mutually_exclusive_group()
    which.add_argument("--check", metavar="PATH", help="a skill directory or .zip, packaged as "
                                                       "upload would")
    which.add_argument("--revision", type=revision_arg,
                       help="a stored revision of the skill: all 64 hex characters")

    args = parser.parse_args(argv)
    if args.command == "draft" and (bool(args.new) != bool(args.project)
                                    or (args.description and not args.new)):
        commands.choices["draft"].error("--new needs --project P; --project and --description "
                                        "go with --new")
    as_json = getattr(args, "json", False)
    try:
        if args.command == "auth":
            auth(args.api, not args.no_browser, args.tracing, args.manage)
        elif args.command == "uninstall":
            uninstall(args.dry_run)
        elif args.command == "init":
            check(args.tracing) if args.check else init(args.dry_run, args.tracing)
        else:
            doc, lines, code = args.run(load_config(), args)
            if as_json:
                print(json.dumps(doc, indent=2))
            else:
                say(*lines)
            return code
    except KeyboardInterrupt:
        say("", "Stopped. Nothing was saved." if args.command == "auth" else
            f"Stopped. Run slancha {args.command} again to finish; it skips what is already done."
            if args.command in ("init", "uninstall") else
            "Stopped waiting; the job goes on (slancha jobs lists it)."
            if getattr(args, "wait", False) else "Stopped.")
        return 130
    except UsageError as exc:
        if as_json:
            print(json.dumps({"error": str(exc), "status": None}))
        else:
            say(str(exc))
        return 2
    except SlanchaError as exc:
        if as_json:
            print(json.dumps({"error": str(exc), "status": getattr(exc, "status", None)}))
        else:
            say(str(exc))
        unmet = isinstance(exc, ApiError) and exc.status == 409 \
            and exc.detail.startswith(CRITERIA_UNMET)
        return CRITERIA_FAILED if unmet else 1
    return 0


# --- command line --------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SlanchaError(f"{message}. Usage: /slancha:update [skill] [--dry-run] [--force]")


def main(argv: list[str]) -> int:
    if argv[:1] == ["hook"]:
        # A hook never fails, never blocks the session and prints nothing but its JSON answer.
        try:
            out = hook(sys.stdin.buffer.read())
            if out:
                print(out)
        except Exception:
            pass
        return 0
    if argv[:1] == ["flush"]:
        try:
            print(flush(load_config()))
        except Exception:
            pass
        return 0
    if argv[:1] not in (["update"], ["status"]):        # the terminal commands, and --help
        return cli(argv)
    parser = _Parser(prog="slancha.py", description="Slancha skills for Claude Code.")
    commands = parser.add_subparsers(dest="command")
    up = commands.add_parser("update", help="install or update Slancha skills")
    up.add_argument("skill", nargs="?")
    up.add_argument("--dry-run", action="store_true", help="say what would change; change nothing")
    up.add_argument("--force", action="store_true", help="replace local changes (kept as backup)")
    up.add_argument("--project", default="")
    st = commands.add_parser("status", help="show the token, the API and the installed skills")
    st.add_argument("--project", default="")
    # Everything below exits 0 with a message: a non-zero exit would make Claude Code abort the
    # command and hide the explanation.
    try:
        args = parser.parse_args(argv)
        cfg = load_config()
        project = project_dir(args.project)
        if args.command == "status":
            lines = status(cfg, project)
        else:
            if args.skill is not None and not valid_name(args.skill):
                raise SlanchaError(f"{args.skill!r} is not a skill name (lowercase letters, "
                                   "digits and single hyphens).")
            lines = update(cfg, project, args.skill, args.dry_run, args.force)
    except SlanchaError as exc:
        lines = [str(exc)]
    except Exception as exc:          # a bug; say so rather than fail the command
        lines = [f"Slancha plugin error: {exc.__class__.__name__}: {exc}"]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        if sys.argv[1:2] not in (["hook"], ["flush"]):    # a hook's stdout can reach Claude
            print("The Slancha plugin needs Python 3.9 or newer on PATH as python3.")
        sys.exit(0)
    sys.exit(main(sys.argv[1:]))
