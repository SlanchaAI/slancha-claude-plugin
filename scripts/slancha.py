#!/usr/bin/env python3
"""Slancha for Claude Code: install and update the skills your Slancha library publishes, and
report when they are used.

Standard library only, Python 3.9 or newer. The plugin's commands and hooks run it:

    slancha.py update [skill] [--dry-run] [--force] [--project DIR]
    slancha.py status [--project DIR]
    slancha.py hook     a Claude Code hook; the event arrives as JSON on stdin
    slancha.py flush    uploads the usage spool; the hooks start it in the background

The token is a personal token from the Slancha console, read from SLANCHA_TOKEN or from
~/.config/slancha/token. It goes only to the configured API origin, only over HTTPS, and is never
printed. A redirect is refused rather than followed, because urllib would send the token along.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import shutil
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

VERSION = "0.1.0"
DEFAULT_API_URL = "https://api.slancha.ai"
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


class Offline(SlanchaError):
    """The API could not be reached."""


class ApiError(SlanchaError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Refused(SlanchaError):
    """A download failed a check and was not installed."""


# --- configuration -------------------------------------------------------------------------------

def config_dir() -> Path:
    return Path.home() / ".config" / "slancha"


def cache_dir() -> Path:
    return Path.home() / ".cache" / "slancha"


def user_skills_root() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "skills"


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
    return "".join(c if c.isprintable() else " " for c in detail)[:200]


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
        if error.code == 401:
            message = ("Slancha did not accept the token (HTTP 401). Create a new one in the "
                       "Slancha console (Claude Code page) and store it again.")
        else:
            message = f"Slancha answered HTTP {error.code}" + (f": {detail}" if detail else ".")
        raise ApiError(error.code, message) from None
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        reason = getattr(error, "reason", None) or error.__class__.__name__
        raise Offline(f"Cannot reach {host} ({reason}).") from None


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
        state = "not in your Slancha library" if row is None else "not published"
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
        except SlanchaError as exc:
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


def session_notice(event: dict) -> str | None:
    """At most once a day: which installed skills have a newer published revision."""
    stamp = cache_dir() / "update-check.stamp"
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
    library = fetch_library(cfg, timeout=3)
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
        if args.command is None:
            raise SlanchaError("Usage: slancha.py update [skill] [--dry-run] [--force] | status")
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
