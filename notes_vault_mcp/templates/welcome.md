---
title: Welcome to your vault
date: {{DATE}}
updated: {{DATE}}
tags: [vault, howto]
status: active
kind: howto
summary: What this vault is, how Claude uses it, and how to open it in Obsidian.
---

## What this is

Your own notes, private to you. Claude reads and writes them through the vault
tools: one living note per system or subsystem in `Areas/`, open work in
`Projects/`, gotchas and how-tos in `Resources/`, one log per repo in `Log/`,
history in `Archive/`. The schema in `.vault/schema.yml` describes the
frontmatter and the tag vocabulary; it is yours to edit.

Start a session with `context` (the working directory and repo name). Before
the first code change, make sure a note for the work exists. End a session
with `log_append`, one line per repo with the commits it produced.

## Open it in Obsidian

1. Create an empty vault in Obsidian and install the community plugin
   **Remotely Save**.
2. In Remotely Save, choose **WebDAV** as the remote. Server address:
   `{{DAV_URL}}`. Username: your email address.
3. For the password, ask Claude to run the `obsidian_access` tool. It shows a
   personal token once. Paste it as the password, then run
   "Check connectivity".
4. Sync. Two-way sync is safe: every write Claude makes carries an etag, and
   Remotely Save keeps its own change log.

Lost the token? Ask Claude to run `obsidian_access` again. The old token
stops working and the new one is shown once.
