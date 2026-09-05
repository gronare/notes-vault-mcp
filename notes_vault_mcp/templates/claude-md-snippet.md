## Vault

The vault is the single source of truth for plans, decisions, progress and reference material.
Never write markdown documentation into a git repo; it goes in the vault.

- **Session start** — call `context` with the working directory and repo name. Read what it returns
  before touching code.
- **Write gate** — a note for the current work must exist before the first Edit, Write or Bash call
  that modifies a file. Searching does not satisfy the gate; the note must be written.
- **Code is truth** — a note is a lead, not a specification. Follow its `path` field, read the code,
  and run `git log --oneline --since=<updated> -- <path>` before repeating any claim it makes. When
  the code disproves a note, correct the note in the same pass.
- **Backlog** — the moment something is deferred, in so many words or in passing ("later", "not
  now", "put it in the backlog"), call `backlog_add` in the same turn and confirm it in one line. A
  backlog note is a decision to build something later; the system note's remaining-work section
  describes what is missing today.
- **Session end** — `log_append` one line per repo, with the commits it produced.
- **Finishing** — `close` the note in the same pass as the last commit of that work.
- **Never report vault bookkeeping** — filenames, statuses and folders mean nothing to the reader.
