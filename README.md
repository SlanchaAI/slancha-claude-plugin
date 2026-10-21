# Slancha for Claude Code

Keeps the skills your [Slancha](https://slancha.ai) library publishes up to date in Claude Code,
and tells Slancha which revision you used, so the console can compare a skill before and after
an update.

- `/slancha:update` updates every Slancha skill you have installed.
- `/slancha:update <skill>` installs one from your library.
- `/slancha:status` shows your token, your installed Slancha skills and pending usage reports.
- Once a day, a new session tells you when an update is available.
- From a terminal, `slancha` also lets you or a coding agent check, upload and review skills
  and follow their jobs ([agent commands](#agent-commands)), and set up tracing of your Claude
  Code sessions to your org's Langfuse project ([tracing](#tracing)).

It needs `python3` 3.9 or newer on your `PATH` (macOS, Linux or WSL). It installs nothing else.

## Set up in one line

To set up a machine for Slancha (this plugin; tracing is a separate, optional step), run as
yourself, not as root:

```sh
curl -fsSL https://dev.slancha.ai/install.sh | SLANCHA_API_URL=<your Slancha API> sh
```

Copy the exact line from the Claude Code page of your Slancha console: it names your API. (The
production URL comes later.) It says what it will do first, then:

1. downloads `scripts/slancha.py` from this repository at a pinned commit and checks its sha256;
2. installs it as `~/.local/share/slancha/slancha.py`, with a `~/.local/bin/slancha` command;
3. runs `slancha init --check`: stops if `claude` or `git` is missing, before anyone approves a
   sign-in;
4. runs `slancha auth`: prints a code and a link, and opens the link in your browser if there is
   one. You approve the code in the console, signed in to your org; the token is yours. Nothing
   is pasted anywhere. It saves, readable only by you (mode 0600, in a 0700 directory):
   - `~/.config/slancha/token`: your personal token;
   - `~/.config/slancha/config.json`: `api_url`, the API you signed in to;
   - `~/.config/slancha/tracing.json`: your org's Langfuse host and keys, only after
     `slancha auth --tracing` (a later sign-in without `--tracing` removes it);
5. runs `slancha init`, which needs `claude` and `git` on `PATH` and installs this plugin
   (`slancha@slancha`). It touches nothing of tracing.

Restart Claude Code afterwards.

To do the same by hand, with `scripts/slancha.py` from this repository:

```sh
python3 scripts/slancha.py init --check     # claude and git are there
python3 scripts/slancha.py auth --api URL    # --no-browser to only print the link
python3 scripts/slancha.py init --dry-run    # says what it would change
python3 scripts/slancha.py init
```

To undo it, run `slancha uninstall` (`--dry-run` first says what it would do). It removes both
plugins and their marketplaces, Slancha's entries in `settings.json` (keeping any value it did
not write), the token, the API setting, the tracing keys, the usage cache and the CLI. Skills it
installed stay where they are and stop updating. Revoke the token in the Slancha console.

`init` is safe to run again: it skips what is already done.

## Tracing

Tracing sends every Claude Code session on the machine, prompts and answers included, to your
org's Langfuse project. An org admin connects that project in the console first. Then:

```sh
slancha auth --tracing     # also asks for the org's tracing keys; an org admin approves
slancha init --tracing     # also needs uv and a Python 3.10 or newer that uv can find
```

`init --tracing` does what `init` does, and also:

- clones [Langfuse's Claude Code plugin](https://github.com/langfuse/claude-observability-plugin)
  into `~/.local/share/slancha/langfuse-observability`, checks out the pinned commit `b5211009`,
  and installs it from there (`langfuse-observability@langfuse-observability`);
- merges into `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`), keeping
  everything else: `env` gets `CC_LANGFUSE_PUBLIC_KEY`, `CC_LANGFUSE_SECRET_KEY` and
  `CC_LANGFUSE_BASE_URL`, and loses any `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
  `LANGFUSE_HOST` or `LANGFUSE_BASE_URL` (the SDK's own names would reroute the traces of every
  program Claude Code starts); the Langfuse plugin's options get skill tags on and image upload
  off. The file then holds a secret, so it is rewritten with mode 0600.

It refuses a `langfuse-observability` marketplace that is not the pinned clone, and warns about a
Langfuse hook you added to `settings.json` by hand (it would send every turn twice).

Afterwards:

| Command | What it does |
|---|---|
| `slancha tracing status [--json]` | the Langfuse plugin (installed, enabled), the clone at the pinned commit, the host and options in `settings.json` (never the secret), and what your workspace receives: traces, per-skill counts, projects. The workspace part needs an agent token (`slancha auth --manage`); without one it says so and shows the rest. |
| `slancha tracing off [--project]` | `claude plugin disable langfuse-observability@langfuse-observability --scope user`; with `--project`, `--scope local` instead: only the project in the current directory, in its `.claude/settings.local.json`, which is not committed |
| `slancha tracing on [--project]` | the same with `claude plugin enable` |
| `slancha tracing config [--max-chars N] [--images on\|off] [--json]` | without options, shows the Langfuse plugin's options; with them, sets `CC_LANGFUSE_MAX_CHARS` and `CC_LANGFUSE_CAPTURE_IMAGES` in `settings.json` (mode 0600). Skill tags stay on: Slancha counts traces per skill by them, so any change turns `CC_LANGFUSE_SKILL_TAGS` back on. |

New Claude Code sessions follow `on`, `off` and `config`; restart the running ones.

## Agent commands

`slancha auth --manage` signs in an **agent token** instead of a plain one. It acts as whoever
approves it, at most as a proposer: it can upload, check, scan and draft, and it can never
approve, reject or roll back a change, or set acceptance criteria. A proposer, approver or admin
approves it. It expires 30 days after it is issued. It replaces `~/.config/slancha/token`, so the
plugin's hooks use it too: once it expires, a new session says so once, and you run
`slancha auth` (or `slancha auth --manage`) again. `slancha status` and `slancha whoami` show
the token's capabilities and expiry.

| Command | Route |
|---|---|
| `slancha whoami` | `GET /api/me`: user, role, capabilities, expiry |
| `slancha skills` | `GET /api/skills` |
| `slancha skill <s>` | `GET /api/skills/<s>/versions` and `/criteria`; `--json` prints `{"versions", "criteria"}` |
| `slancha upload <path> [path ...] [--name N] [--dry-run]` | `POST /api/skills/upload` once per skill. A path is a directory of text files (hidden files, `__pycache__` and `slancha-manifest.json` left out) or a `.zip` (sent as it is, after the same limits). A directory with no `SKILL.md` is a folder of skills: each child directory that has `SKILL.md`, and each `.zip`, is uploaded on its own. `--name` only with exactly one skill. `--dry-run` packages, validates and runs the criteria check, and uploads nothing. |
| `slancha pending [<s>]` | `GET /api/pending`, or one skill's review card |
| `slancha review <s>` | the review card, `GET /api/evidence/<s>` and every changed component's diff, page by page; `--json` prints `{"card", "evidence", "diffs"}`. Read-only. |
| `slancha jobs [--skill S]` | `GET /api/jobs` |
| `slancha job <id> [--wait] [--cancel]` | `GET /api/jobs/<id>`; `--cancel` first cancels it; `--wait` polls until it ends |
| `slancha scan <s> [--revision R] [--wait]` | `POST /api/skills/<s>/scan`: the waiting revision, else the served one, or `R` |
| `slancha draft <s> [--new --project P [--description D]]` | `POST /api/draft/<s>` |
| `slancha outcomes <s> [--days N] [--start]` | `GET /api/skills/<s>/outcomes` (1 to 90 days, default 30), or `--start` an outcomes job |
| `slancha criteria <s> [--check PATH \| --revision R]` | `GET /api/skills/<s>/criteria`, or `POST .../criteria/check` with a package (packaged as `upload` does) or a stored revision; stores nothing |

`slancha draft` sends scrubbed excerpts of the workspace's traced sessions to a model on the
workspace's own OpenAI key. It works only after an admin lets agent tokens start drafts and
outcome jobs in the console's Workspace settings; that opt-in is the consent, and the command
always sends `acknowledge: "traces-to-model-provider-v1"`. `outcomes` needs the same opt-in.

Every command takes `--json`: stdout then holds only JSON, the API's answer or the wrapper named
above, and an error is `{"error": "<message>", "status": <HTTP status or null>}`. Exit codes:

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | an error or a refusal, including a job that `--wait` saw fail or be cancelled, and an upload whose security scan did not start (it is stored: run `slancha scan <s> --wait`, not another upload) |
| 2 | a usage error |
| 3 | the acceptance criteria are not met: `criteria --check` or `--revision` failed, an upload missed them (it is stored, but cannot be approved), or the API answered 409 `acceptance criteria not met: ...`. `upload` of several skills exits 3 when any one of them does, even if another skill also failed |

Check a package with `slancha criteria <s> --check <dir>` before uploading it: a skill takes no
other upload while a change waits for review, and the CLI says so (it does not send the upload).
`slancha upload` keeps going after a failure and prints one result per skill. With one skill path,
`--json` is the API's object; with more than one, or a folder of skills, it is an array of
`{path, skill, exit, uploaded, result|error}`. A single `--dry-run` prints one object with
`dry_run` and `criteria`. The plugin's `slancha` skill tells Claude Code the same.

## Install

In Claude Code:

```
/plugin marketplace add SlanchaAI/slancha-claude-plugin
/plugin install slancha@slancha
```

Or from your shell: `claude plugin marketplace add SlanchaAI/slancha-claude-plugin`, then
`claude plugin install slancha@slancha`.

## Connect it to Slancha

1. In the Slancha console, open the **Claude Code** page and create a personal token. It starts
   with `slancha_pat_` and is shown once.
2. Store it from your own terminal, not from Claude Code (never paste it into the chat or run it
   with `!`):

   ```sh
   umask 077; mkdir -p ~/.config/slancha; printf '%s' '<token>' > ~/.config/slancha/token
   printf '{"api_url": "%s"}\n' '<your Slancha API>' > ~/.config/slancha/config.json
   ```

   The second line tells the plugin which API the token belongs to; the console's Claude Code
   page shows both lines with your API filled in. Start the lines with a space if your shell
   leaves such lines out of its history. You can set `SLANCHA_TOKEN` and `SLANCHA_API_URL` in your
   environment instead.
3. In Claude Code, run `/slancha:update <skill>` to install a skill, and `/slancha:update` later
   to update it.

A personal token can read your org's published skills and report their use. It cannot change
anything in your library. Revoke it on the same console page. An agent token can propose
changes; see [agent commands](#agent-commands).

## Updating skills

Slancha skills live where Claude Code reads skills: `~/.claude/skills/<skill>`, or a project's
`.claude/skills/<skill>` if your team committed one there. Each carries a `slancha-manifest.json`
that lists the revision and the sha256 of every file.

`/slancha:update [skill] [--dry-run] [--force]`:

- compares each installed skill with the revision your library serves;
- refuses to replace a skill you edited (a file changed, added or removed), unless you pass
  `--force`;
- downloads over HTTPS from the Slancha API only, and never follows a redirect;
- checks the zip's sha256, unpacks it into a temporary directory under the same bounds the
  server applies to uploads (at most 200 files, 256 KiB per file, 2 MiB in all; no links, no
  absolute or `..` paths), and checks every file against the manifest;
- swaps the new directory in, keeping the previous copy in `.slancha-backup/<skill>` beside it
  (one backup per skill).

`--dry-run` says what would change and changes nothing. Claude Code picks up changed and newly
added skills by itself. The exception is a session that started before `~/.claude/skills`
existed: Claude Code is not watching that directory yet, so when the plugin creates it, it tells
you to run `/reload-skills`.

## Usage reports

When Claude uses a Slancha skill, or you type `/<skill>`, the plugin writes one line to
`~/.cache/slancha/usage.jsonl` (readable only by you) and uploads it when Claude finishes a turn
or the session ends. Each event holds:

- the skill name and its installed revision (`unknown` once you have edited the files);
- whether the call succeeded, when Claude Code says;
- the sha256 of the Claude Code session id (never the id itself);
- the time, and an event id derived from the hashed session and the tool call or prompt, so a
  retried upload or a hook that fires twice is counted once.

It never holds your prompts, the skill's arguments, file contents or paths. Skills that did not
come from Slancha are not reported. When the API is out of reach the events wait in the file;
events older than 30 days are dropped. `/slancha:status` shows how many are waiting and how the
last upload went.

## Configuration

| Setting | Default |
|---|---|
| `SLANCHA_TOKEN`, or `~/.config/slancha/token` | none: `/slancha:update` explains how to add one |
| `SLANCHA_API_URL`, or `{"api_url": "..."}` in `~/.config/slancha/config.json` | `https://api.slancha.ai`, which does not serve Slancha yet: use the API your console shows |

Only `https://` URLs are accepted (`http://` only for `127.0.0.1` and `localhost`).

## Development

Everything runs in Docker:

```sh
scripts/test.sh          # unit tests against a fake Slancha API, Python 3.9 and 3.12
scripts/e2e.sh           # claude plugin validate, then the plugin inside the real Claude Code
                         # binary against a fake model and the fake API
scripts/prepush_scan.sh  # run before every push: credentials, internal hosts, private names
```

## License

Apache-2.0. See [LICENSE](LICENSE).
