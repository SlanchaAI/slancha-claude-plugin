---
name: slancha
description: Work with the team's Slancha skill library from the terminal with the slancha CLI. List skills, check a skill package against its acceptance criteria, upload a change for review, read what waits for review, and follow scan, draft and outcomes jobs. Use when asked to publish, propose, update, check or review a Slancha skill.
---

# The slancha CLI

Run it as `slancha <command>`. If `slancha` is not on PATH, run
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/slancha.py" <command>` instead.

Most commands need an agent token. `slancha whoami` shows the token's capabilities. If `manage`
is missing, or a command says "This needs an agent token", ask the user to run
`slancha auth --manage` in their own terminal; they approve it in the Slancha console. An agent
token expires after 30 days, and then commands say "Your Slancha token expired". Never read,
print or handle the token yourself.

## Commands

| Command | What it does |
|---|---|
| `slancha whoami` | the token's user, role, capabilities and expiry |
| `slancha skills` | the library: served revision, waiting changes, uses in 7 days |
| `slancha skill <s>` | publications, the waiting change, acceptance criteria |
| `slancha criteria <s>` | the rules, and how the served and waiting revisions fare |
| `slancha criteria <s> --check <dir or .zip>` | check a package against them; stores nothing |
| `slancha criteria <s> --revision <hex>` | check a stored revision |
| `slancha upload <dir or .zip> [--name N]` | propose the package for review; starts a security scan |
| `slancha pending [<s>]` | what waits for review, or one skill's review card |
| `slancha review <s>` | the waiting change's card, evidence and every diff; read-only |
| `slancha jobs [--skill S]` | recent scan, draft and outcomes jobs |
| `slancha job <id> [--wait] [--cancel]` | one job; `--wait` polls until it ends |
| `slancha scan <s> [--revision R] [--wait]` | scan a stored revision again |
| `slancha draft <s>`, `slancha draft <s> --new --project P` | draft from traced sessions (below) |
| `slancha outcomes <s> [--days N] [--start]` | how the sessions that ran each revision ended |

Every command takes `--json`. Stdout then holds only JSON: the API's answer, or
`{"versions", "criteria"}` for `skill` and `{"card", "evidence", "diffs"}` for `review`. An error
is `{"error": "<message>", "status": <HTTP status or null>}`.

Exit codes: 0 done; 1 an error or a refusal (the message says why); 2 a usage error; 3 the
acceptance criteria are not met. `job --wait` and `scan --wait` exit 1 unless the job succeeded.

## Proposing a change

1. Work in a directory with `SKILL.md` at its top; its frontmatter `name` is the skill's name.
2. Run `slancha criteria <s> --check <dir>` and fix every ✗ until it exits 0.
3. Run `slancha upload <dir>`. Exit 3 means it misses the criteria: the change is stored but
   cannot be approved, and the skill takes no other upload until a person rejects it. That is
   why step 2 comes first.
4. Follow the security scan with `slancha job <scan job id> --wait`.
5. Tell the user the change waits for review in the Slancha console.

An upload leaves out hidden files, `__pycache__` and `slancha-manifest.json`. A skill holds text
files only: at most 200 files, 256 KB each and 2 MB in all.

## What stays with people

Approving, rejecting and rolling back changes, and setting acceptance criteria, happen in the
console. No command does them, and an agent token cannot. Do not look for a way around that.

## Drafts send traces to a model

`slancha draft` starts a job in which scrubbed excerpts of the workspace's traced Claude Code
sessions go to a model on the workspace's own OpenAI key. It works only after an admin lets agent
tokens start drafts and outcome jobs in Workspace settings; that opt-in is the consent the
command sends. Ask the user before you start one. A draft ends as a change waiting for review.
`--new --project P` writes a new skill from one project's sessions; `slancha tracing status`
lists the projects. `slancha outcomes <s> --start` needs the same opt-in.
