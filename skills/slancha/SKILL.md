---
name: slancha
description: Work with the team's Slancha skill library from the terminal with the slancha CLI. List skills, check a skill package against its acceptance criteria, upload a change for review, read what waits for review, follow scan, draft and outcomes jobs, and check or set up the trace logging of Claude Code sessions to Langfuse. Use when asked to publish, propose, update, check or review a Slancha skill, or about Slancha tracing.
---

# The slancha CLI

Run it as `slancha <command>`. If `slancha` is not on PATH, run
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/slancha.py" <command>` instead.

Most commands need an agent token. `slancha whoami` shows the workspace, the token's role,
capabilities and expiry, and what it can do. If `manage` is missing, or a command says "This
needs an agent token", ask the user to run `slancha auth --manage` in their own terminal; they
approve it in the Slancha console. An agent token expires after 30 days, and then commands say
"Your Slancha token expired". Never read, print or handle the token yourself.

## Commands

| Command | What it does |
|---|---|
| `slancha whoami` | the workspace, the token's user, role, capabilities and expiry |
| `slancha skills` | the library: served revision, waiting changes, uses in 7 days |
| `slancha skill <s>` | publications, the waiting change, acceptance criteria |
| `slancha criteria <s>` | the rules, and how the served and waiting revisions fare |
| `slancha criteria <s> --check <dir or .zip>` | check a package against them; stores nothing |
| `slancha criteria <s> --revision <hex>` | check a stored revision |
| `slancha upload <dir or .zip> [--name N]` | propose the package for review; starts a security scan |
| `slancha pending [<s>]` | what waits for review, or one skill's review card and gate |
| `slancha review <s>` | the waiting change's card, evidence and every diff; read-only |
| `slancha jobs [--skill S]` | recent scan, draft and outcomes jobs |
| `slancha job <id> [--wait] [--cancel]` | one job; `--wait` polls until it ends |
| `slancha scan <s> [--revision R] [--wait]` | scan a stored revision again |
| `slancha draft <s>`, `slancha draft <s> --new --project P` | draft from traced sessions (below) |
| `slancha outcomes <s> [--days N] [--start]` | how the sessions that ran each revision ended |
| `slancha tracing status` | whether this machine traces its sessions, and what the workspace receives |
| `slancha tracing on [--project]`, `off [--project]` | turn tracing on or off (below) |
| `slancha tracing config [--max-chars N] [--images on\|off]` | show or set what a trace captures |

A `--revision` takes all 64 hex characters, as `--json` shows them; the text output shortens
revisions to 12.

Every command takes `--json`. Stdout then holds only JSON: the API's answer, or
`{"versions", "criteria"}` for `skill` and `{"card", "evidence", "diffs"}` for `review`. An error
is `{"error": "<message>", "status": <HTTP status or null>}`. In `skills --json`, each row has
`name`, `revision` (the served one, `""` when none), `pending` (true while a change waits for
review), `pending_kind`, `uses_7d` and `scan`.

Exit codes: 0 done; 1 an error or a refusal (the message says why); 2 a usage error; 3 the
acceptance criteria are not met. `job --wait` and `scan --wait` exit 1 unless the job succeeded.
An HTTP 5xx is Slancha's side, not your package: tell the user rather than retrying in a loop.

## Proposing a change

1. Work in a directory with `SKILL.md` at its top; its frontmatter `name` is the skill's name.
2. Run `slancha criteria <s>` to see the rules, then `slancha criteria <s> --check <dir>`, and fix
   every ✗ until it exits 0. Keep what the skill does; tell the user what you changed.
3. Run `slancha upload <dir>`. Once it prints `Uploaded`, the change is stored, and the skill takes
   no other upload until a person approves or rejects it, whatever the exit code:
   - exit 3: it misses the criteria and cannot be approved; a person must reject it. That is why
     step 2 comes first.
   - exit 1: its security scan did not start. Run `slancha scan <s> --wait`; do not upload again.
4. Follow the security scan with `slancha job <scan job id> --wait`.
5. Run `slancha pending <s>`. `Gate: blocked` lists what stops approval; warnings never block.
   The `no source declared` and `no license declared` warnings go away with `source:` and
   `license:` in `SKILL.md`'s frontmatter, if the user tells you what they are.
6. Tell the user the change waits for review in the Slancha console.

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
Without `--new` it improves a skill that is served and has no change waiting.
`--new --project P` writes a new skill from one project's sessions; `slancha tracing status`
lists the projects. `slancha outcomes <s> --start` needs the same opt-in.

## Tracing

Tracing sends this machine's Claude Code sessions to the workspace's Langfuse project, which is
what drafts and outcomes read. `slancha tracing status` shows what is set up. Setting it up takes
the user, in this order:

1. An admin connects the workspace's Langfuse project in the console (status says "Tracing is
   not connected" until then).
2. The user runs `slancha auth --tracing` in their own terminal; an org admin approves it.
3. The user runs `slancha init --tracing` (it needs `uv`).

After that, `slancha tracing on --project` or `off --project` turns it on or off for the project
in the current directory only (in `.claude/settings.local.json`, which is not committed); without
`--project` it applies to every project. Ask the user before you turn tracing on or set
`tracing config --images on`: both send more of their sessions out. Skill tags stay on, because
Slancha counts traces per skill by them.
