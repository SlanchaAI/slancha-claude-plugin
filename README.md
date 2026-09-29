# Slancha for Claude Code

Keeps the skills your [Slancha](https://slancha.ai) library publishes up to date in Claude Code,
and tells Slancha which revision you used, so the console can compare a skill before and after
an update.

- `/slancha:update` updates every Slancha skill you have installed.
- `/slancha:update <skill>` installs one from your library.
- `/slancha:status` shows your token, your installed Slancha skills and pending usage reports.
- Once a day, a new session tells you when an update is available.

It needs `python3` 3.9 or newer on your `PATH` (macOS, Linux or WSL). It installs nothing else.

## Set up in one line

To set up a machine for Slancha (this plugin, and tracing of your Claude Code sessions to your
org's Langfuse project), run as yourself, not as root:

```sh
curl -fsSL https://dev.slancha.ai/install.sh | sh
```

(The production URL comes later.) It says what it will do first, then:

1. downloads `scripts/slancha.py` from this repository at a pinned commit and checks its sha256;
2. installs it as `~/.local/share/slancha/slancha.py`, with a `~/.local/bin/slancha` command;
3. runs `slancha auth`: prints a code and a link, and opens the link in your browser if there is
   one. An admin of your Slancha org approves the code in the console; nothing is pasted
   anywhere. It saves, readable only by you (mode 0600, in a 0700 directory):
   - `~/.config/slancha/token`: your personal token;
   - `~/.config/slancha/config.json`: `api_url`, the API you signed in to;
   - `~/.config/slancha/tracing.json`: your org's Langfuse host and keys, when it has them;
4. runs `slancha init`, which needs `claude`, `git` and `uv` on `PATH` and a Python 3.10 or newer
   that uv can find (it says which are missing):
   - clones [Langfuse's Claude Code plugin](https://github.com/langfuse/claude-observability-plugin)
     into `~/.local/share/slancha/langfuse-observability`, checks out the pinned commit
     `b5211009`, and installs it from there (`langfuse-observability@langfuse-observability`);
   - installs this plugin (`slancha@slancha`);
   - merges into `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`), keeping
     everything else: `env` gets `CC_LANGFUSE_PUBLIC_KEY`, `CC_LANGFUSE_SECRET_KEY` and
     `CC_LANGFUSE_BASE_URL`, and loses any `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
     `LANGFUSE_HOST` or `LANGFUSE_BASE_URL` (the SDK's own names would reroute the traces of
     every program Claude Code starts); the Langfuse plugin's options get skill tags on and
     image upload off. The file then holds a secret, so it is rewritten with mode 0600.

Restart Claude Code afterwards. `SLANCHA_API_URL` picks another Slancha API.

To do the same by hand, with `scripts/slancha.py` from this repository:

```sh
python3 scripts/slancha.py auth              # --api URL, --no-browser
python3 scripts/slancha.py init --dry-run    # says what it would change
python3 scripts/slancha.py init
```

`init` is safe to run again: it skips what is already done. It refuses a `langfuse-observability`
marketplace that is not the pinned clone, and warns about a Langfuse hook you added to
`settings.json` by hand (it would send every turn twice).

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
   ```

   Start the line with a space if your shell leaves such lines out of its history. You can set
   `SLANCHA_TOKEN` in your environment instead.
3. In Claude Code, run `/slancha:update <skill>` to install a skill, and `/slancha:update` later
   to update it.

A personal token can read your org's published skills and report their use. It cannot change
anything in your library. Revoke it on the same console page.

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
| `SLANCHA_API_URL`, or `{"api_url": "..."}` in `~/.config/slancha/config.json` | `https://api.slancha.ai` |

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
