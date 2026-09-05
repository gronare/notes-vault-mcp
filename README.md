<!-- mcp-name: io.github.gronare/notes-vault-mcp -->

# notes-vault-mcp

An MCP server for a vault of markdown notes — the kind Obsidian keeps: a folder of `.md` files with
YAML frontmatter. The vault lives either in a local directory or in an S3 bucket (MinIO included),
and the server gives an agent a cheap, indexed way to read and write it.

The point is that an agent should be able to answer "what do we already know about this?" in one
call, and should be told when the vault has drifted away from the code. So the server does more than
read and write files:

- **A local SQLite index.** Every tool call refreshes it, fetching only the notes whose version
  changed. Search never downloads the vault.
- **Full-text search with BM25 ranking**, folder weights, recency decay and a status factor, so the
  living system note outranks a two-year-old archived plan on the same words.
- **A schema.** The frontmatter contract lives in the vault as `.vault/schema.yml`: which folders
  exist and what each is for, which fields are required, which statuses and kinds are legal, which
  folders must link an `area`. Writes are validated against it and refused when they do not hold.
- **A lifecycle.** `close` archives a finished note and stamps its status; `log_append` writes one
  dated line per repo per session; `lint` reports every kind of drift it can see.
- **Session hooks** for Claude Code: `session-start` hands the agent the system notes for the repo it
  is about to touch — plus the commits made since each note was last updated — and `stop` refuses to
  end a session that left commits unlogged or notes stale.

Swedish or English notes both work: the index folds diacritics, and the schema carries a synonym list
so `bokning` finds `booking`.

## Install

### As a Claude Code plugin

```sh
claude plugin marketplace add https://github.com/gronare/claude-plugins
claude plugin install vault@gronare
```

The plugin asks for the vault settings and passes them as `CLAUDE_PLUGIN_OPTION_*` environment
variables, which this server reads as if they were the bare names.

### As an MCP server, straight from PyPI

```sh
claude mcp add vault -s user \
  -e VAULT_PATH=$HOME/vault \
  -- uvx notes-vault-mcp
```

Or against S3 / MinIO:

```sh
claude mcp add vault -s user \
  -e S3_ENDPOINT=https://minio.example.com \
  -e S3_ACCESS_KEY=... \
  -e S3_SECRET_KEY=... \
  -e S3_BUCKET=vault \
  -- uvx notes-vault-mcp
```

### As a container

```sh
claude mcp add vault -s user -- \
  docker run --rm -i \
  -e S3_ENDPOINT -e S3_ACCESS_KEY -e S3_SECRET_KEY -e S3_BUCKET \
  ghcr.io/gronare/notes-vault-mcp:latest
```

## Configuration

Every variable is also read from `CLAUDE_PLUGIN_OPTION_<NAME>`, which is how the Claude Code plugin
passes its user config. The bare name wins when both are set.

| Variable | Required | Meaning |
| --- | --- | --- |
| `VAULT_PATH` | for a local vault | Directory holding the vault. Selects the local backend. |
| `S3_ENDPOINT` | for an S3 vault | Endpoint URL, e.g. `https://minio.example.com`. |
| `S3_ACCESS_KEY` | for an S3 vault | Access key. |
| `S3_SECRET_KEY` | for an S3 vault | Secret key. |
| `S3_BUCKET` | for an S3 vault | Bucket holding the vault. |
| `S3_PREFIX` | no | Key prefix inside the bucket. |
| `S3_REGION` | no | Region, default `us-east-1`. |
| `VAULT_CACHE_DIR` | no | Where the index lives, default `~/.cache/notes-vault-mcp`. |
| `VAULT_SCHEMA` | no | Local path to a schema file, overriding the one in the vault. |
| `VAULT_TOKEN` | for HTTP | Bearer token. Required by `--transport http`. |
| `VAULT_STOP_HOOK` | no | `off` disables the stop hook. |

Set `VAULT_PATH` **or** the four `S3_*` variables. With neither, the server exits with one line
saying so.

## First run

```sh
uvx notes-vault-mcp init
```

`init` writes into the vault, and refuses to overwrite anything without `--force`:

- `.vault/schema.yml` — the frontmatter contract, copied from the built-in default so you can edit it.
- `Areas.base`, `Open tasks.base`, `Resources.base` — Obsidian Bases views over the same structure.

It then prints a CLAUDE.md snippet to stdout: the workflow rules an agent needs on its side of the
conversation.

## The schema

`.vault/schema.yml` is deep-merged over the built-in default, so it only needs to carry what differs.
The default lays out five folders:

| Folder | Kind | Weight | Role |
| --- | --- | --- | --- |
| `Areas/` | system | 3.0 | One living note per system. Current state only. The hubs of the graph. |
| `Resources/` | reference | 2.0 | Traps, how-tos and decisions with their reasons. |
| `Projects/` | task | 1.0 | Open work spanning sessions. Closed with `close`. |
| `Log/` | log | 1.0 | Append-only log per repo, one note per repo. |
| `Archive/` | archive | 0.3 | History. Searched only on request. |

and the contract for a note:

```yaml
frontmatter:
  required: [title, date, updated, tags, status]
  optional: [kind, area, summary, path, superseded_by]
  area_required_in: [Projects, Resources, Log]
  status_values: [draft, active, complete, superseded]
  kind_values: [system, task, trap, howto, decision, reference, log]
```

`path` is what ties a note to code: a comma-separated list of directories (`~` is kept as written and
also indexed expanded). That is what `context` and the session hook match against.

The repo log is one note per repo, and both its filename and its line format are schema settings:

```yaml
log:
  folder: Log
  file_format: "{repo}-log.md"
  entry_format: "- [{date}] {line} | commits: {commits} | {area}"
```

`file_format` takes a single `{repo}` placeholder, and the default suffix is what keeps the log clear
of the hub note: with `Areas/greenhouse.md` and `Log/greenhouse.md` both in the vault, Obsidian cannot
resolve `[[greenhouse]]`. Every place that builds the log path reads this setting — `log_append`, the
log tail in `context`, the stop hook's unlogged-commit check, `changelog` and `lint` — so changing it
moves all of them at once. Rename the existing files to match when you change it.

Also configurable: the tag vocabulary and whether it is enforced, the synonym groups search expands,
`stale_after_days`, and the search weights.

## Tools

Every call refreshes the index first, throttled to at most once every 20 seconds.

| Tool | Cost | What it does |
| --- | --- | --- |
| `search` | cheap | Full-text over the index. Title, summary, tags and body, with synonyms, prefixes, quoted phrases and folded diacritics. A bare commit sha finds the notes that mention it. Hides archive and superseded notes and says how many. |
| `context` | cheap | The session-start call: the system notes covering a path, the open tasks, the reference notes and the tail of the repo log, in one answer. |
| `list_files` | cheap | Paths only. |
| `read_file` | moderate | One note, prefixed with `etag: <version>`. A superseded note carries a warning callout. |
| `lint` | moderate | Reads every note and reports drift. |
| `write_file` | write | Validates against the schema and refuses the write if it does not hold. Stamps `updated`, fills `date`. Pass `expected_etag` to make the write conditional. |
| `append_file` | write | Appends and bumps `updated`. Creates the note when missing. |
| `close` | write | Sets status complete (or superseded, with `superseded_by`, when `merged_into` is given) and moves the note into the archive. |
| `log_append` | write | One dated line in the repo log, with the commits it produced. Creates the log when missing. |
| `move_file` | write | Moves or renames. |
| `delete_file` | write | Deletes for good. Prefer `close`. |

`search` filters: `folder`, `status`, `tag`, `kind`, `area`, `path_prefix`, `since`,
`include_archive`, `include_superseded`, `limit`.

### What lint reports

`broken_frontmatter`, `missing_required` (per field), `missing_area`, `unknown_tags` (only when the
vocabulary is strict), `unresolved_links`, `orphans` (no inbound wikilink; log and archive ignored),
`stale_active`, `archive_status_mismatch`, `duplicate_stems`, `superseded_target_missing`.

```sh
uvx notes-vault-mcp lint
uvx notes-vault-mcp lint --write "Log/lint-$(date +%F).md"
```

## Hooks

Two Claude Code hooks, both reading the hook JSON on stdin and both exiting 0 whatever happens.

`session-start` prints the context bundle for the working directory, then — for each system note it
returned — the commits touching that note's `path` since the note was last updated. That is the
answer to "is this note still true?" before the agent believes it.

`stop` blocks the end of a session that left work unrecorded: commits from the last 24 hours whose
sha does not appear in the repo log, and open task notes older than 14 days. It returns
`{"decision": "block", "reason": ...}`, or nothing at all when the vault is up to date. Set
`VAULT_STOP_HOOK=off` to silence it.

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "uvx notes-vault-mcp hook session-start" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "uvx notes-vault-mcp hook stop" }] }
    ]
  }
}
```

## Other commands

```sh
notes-vault-mcp serve --transport stdio          # the default
notes-vault-mcp sync --rebuild                   # drop the index and read every note again
notes-vault-mcp search "bokning" --limit 5       # the same ranking, from a shell
notes-vault-mcp changelog greenhouse 2026-08 --repo-path ~/projects/greenhouse
notes-vault-mcp changelog greenhouse 2026-08 --repo-path ~/projects/greenhouse --write
notes-vault-mcp changelog --all                  # this month (and last month during its first week)
```

`changelog` prints the log lines, the git commits grouped by day, and the repo's notes dated inside
the period. With `--write` it keeps that as a period page, `Log/<repo>-<period>.md`, between the
markers `<!-- changelog:generated -->` and `<!-- /changelog:generated -->`; prose above the markers
(a summary written by an agent at month end) is left alone, and the page's status follows the
calendar. `--all` does it for every repo the session-start hook has seen on this machine, and the
stop hook runs that once a day, so the pages stay current without a cron.

## Backlog

```sh
notes-vault-mcp backlog --area greenhouse --priority high
```

A backlog item is a task note with `status: backlog`, an `area`, a one-line `summary`, an optional
`priority` (`urgent`, `high`, `medium`, `low`) and an optional `source` (who said it and when, or a
sha). `backlog_add` files one from a conversation the moment something is deferred; `backlog` lists
them by priority then age, filtered by area or family; `context` shows the ones relevant to the
current repo apart from the open tasks. Picking an item up is setting its status to `active`;
finishing it is `close`. Lint leaves backlog notes alone however old they get, and flags a
`complete` note that was never closed. `init` writes `Backlog.base` next to the other Obsidian bases.

## HTTP transport

```sh
VAULT_TOKEN=$(openssl rand -hex 32) notes-vault-mcp serve --transport http --host 0.0.0.0 --port 8765
```

Streamable HTTP on `/mcp`. Every request must carry `Authorization: Bearer $VAULT_TOKEN`; anything
else gets 401 before it reaches the server. `VAULT_TOKEN` is mandatory in this mode — the command
refuses to start without it.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
```

The test suite runs against a fixture vault under `tests/fixtures/vault/` and a moto-mocked S3
bucket. It never touches a real bucket.

## License

MIT. See [LICENSE](LICENSE).
