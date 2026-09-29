#!/usr/bin/env bash
# Run before every push. This repository is public, and a push publishes every commit reachable
# from the pushed branch, so the scan reads the working tree and the whole history. It fails on
# anything shaped like a credential, on internal host names, and on names that must stay private.
# Those names are listed only by their sha256, so this file does not publish them either.
# git runs on the host; the scanner runs in Docker.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
history="$(mktemp)"
trap 'rm -f "$history"' EXIT
git -C "$root" log --all -p --no-color --format='commit %H%n%B' > "$history"
docker run --rm -i --memory 1g -v "$root:/w:ro" -v "$history:/history:ro" -w /w \
  python:3.12-slim python3 - <<'EOF'
import hashlib
import os
import re
import sys

PATTERNS = {
    "a Slancha personal token": r"slancha_pat_[A-Za-z0-9_-]{30,}",
    "an AWS access key": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "an AWS secret key": r"(?i)aws_secret_access_key\s*[=:]\s*\S{20,}",
    "a private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "an Anthropic key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "an OpenRouter key": r"sk-or-v1-[0-9a-f]{20,}",
    "an OpenAI-style key": r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}",
    "a Langfuse key": r"\b[ps]k-lf-[0-9a-f-]{20,}",
    "a GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})",
    "a Slack token": r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
    "a Google API key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "a JWT": r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "an AWS endpoint (Lambda URL, DSQL, ...)": r"(?i)\.on\.aws\b|amazonaws\.com",
    "a tunnel host": r"(?i)ngro[k]|trycloudflare\.com|\.ts\.net\b",
    "a Supabase project host": r"(?i)[a-z0-9]{15,}\.supabase\.(?:co|in)\b",
    "a private IP address": r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b",
}
# (length, sha256) of lowercase names that must not appear: customer names, internal hosts and
# account ids. Any run of letters and digits containing one of them fails.
PRIVATE = [
    (10, "ef76f2b105b355af8e6bd3c1b74b71ad8ebe6e4b4c6afaa416acd4ebbf64a301"),
    (7, "c0d142a8f47ed043a92fd0dc31945d8ffcc6d9ea88e1b3f0538e0c20a15d8217"),
    (9, "88b6667b679952b90d978df7aa5e9f950863d54a1903b98ed958de15034847b3"),
    (12, "a5eafdfd97dc86c9c94d502f66f0d8be585e2604bc0bcc0c85d1fda6612966da"),
    (25, "a8fa41b78287e134a99a52b2c788a1db9e7ee4e1c55174a3edc2647d8303e42c"),
    (8, "0fd16872ecfba3f47ccb8220f4b7ea9ff1471b4547fcf532af51b3a5db7a012a"),
    (10, "cf1c670bac8275d34fc64386da93e30c27ae533a3e3cede5bd12ce1082b57598"),
]
BAD_FILES = re.compile(r"(^|/)(\.env(\..*)?|.*\.pem|.*\.key|id_[a-z0-9]+)$")
compiled = {kind: re.compile(p) for kind, p in PATTERNS.items()}
private = {length: set() for length, _ in PRIVATE}
for length, digest in PRIVATE:
    private[length].add(digest)


def findings(text):
    for kind, pattern in compiled.items():
        if pattern.search(text):
            yield kind
    for run in re.findall(r"[a-z0-9]+", text.lower()):
        for length, digests in private.items():
            for i in range(len(run) - length + 1):
                if hashlib.sha256(run[i:i + length].encode()).hexdigest() in digests:
                    yield "a private name"
                    break


problems = []
for base, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
    for name in files:
        path = os.path.join(base, name)[2:]
        if path == ".git" or BAD_FILES.search(path):
            problems.append(f"{path}: a file that usually holds secrets")
        for kind in set(findings(path)):
            problems.append(f"{path}: {kind} in the file name")
        with open(path, encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                for kind in set(findings(line)):
                    problems.append(f"{path}:{number}: {kind}")

commit = "?"
with open("/history", encoding="utf-8", errors="replace") as handle:
    for line in handle:
        if line.startswith("commit ") and len(line.split()) == 2:
            commit = line.split()[1][:12]
        for kind in set(findings(line)):
            problems.append(f"history {commit}: {kind}")

for problem in sorted(set(problems)):
    print(problem)
print(f"prepush scan: {'clean' if not problems else f'{len(set(problems))} problems; do not push'}")
sys.exit(1 if problems else 0)
EOF
