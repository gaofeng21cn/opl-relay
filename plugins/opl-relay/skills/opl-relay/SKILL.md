---
name: opl-relay
description: Use OPL Relay to inspect configured mailboxes, retrieve approved private memory and Obsidian context, and manage review-gated Apple Mail drafts.
---

# OPL Relay

Use the installed Relay CLI as the authority. The plugin contains capability
instructions only; it never treats the plugin directory as user storage.

## Resolve Runtime

1. Prefer `opl-relay`; use the compatibility command `codex-mail` when needed.
2. Run `opl-relay --json doctor` before mailbox work.
3. Treat `state_dir` as the user-owned long-lived data root and
   `workspace.path` as the active human-editable context.
4. Honor `OPL_RELAY_HOME` and `OPL_RELAY_WORKSPACE`. `CODEX_MAIL_HOME` is a
   compatibility fallback, not the new public contract.
5. Read `<workspace>/AGENTS.md` when it exists, then follow its profile,
   policies, context, skills, and templates references.

Never put accounts, SQLite files, raw mail, sync cursors, memories, private
policies, or credentials in the plugin cache or source checkout. Credentials
remain in macOS Keychain service `codex-mail-workbench` for compatibility.

## Capability Contract

The OPL Package exports four stable capability contracts:

- `communications.mail.v1`: local mailbox evidence, sync, retrieval, and
  review-gated Apple Mail drafts;
- `personal.context.v1`: bounded drafting context assembled from evidence;
- `personal.memory.v1`: proposed and explicitly approved relationship memory;
- `knowledge.obsidian.v1`: read-only indexing and retrieval from configured
  Obsidian sources.

These identifiers describe available contracts. They do not grant new mailbox
writes or move private data into the plugin.

## Mail And Context

Use `accounts` as account truth. Sync explicitly when freshness matters, then
use `recent`, `search`, and `read` through stable `email-store://` references.
Before drafting for a known person or project, run `context build`. Use only
approved memories as active relationship memory and re-read raw mail for
high-risk dates, roles, commitments, or invitations.

## External Web Decisions

For an explicitly authorized action on an authenticated editorial, submission,
or other external website:

1. Resolve the exact target from Relay evidence before opening the site: service,
   manuscript or record id, title, revision date, and requested decision.
2. Prefer Codex browser-client control and an existing authenticated Chrome
   session when available. Honor an explicit browser choice. Use Playwright only
   as a deterministic DOM-inspection or UI-debugging fallback, not as a second
   independent session.
3. Inspect the complete form before submitting. Required fields may include
   author comments, editor notes, ethics answers, reviewer counts, signatures,
   or an explicit decision radio. A selected option or successful click is not
   submission evidence.
4. Handle confirmation dialogs explicitly. Treat the operation as pending until
   a real submit response, navigation, or equivalent owner-surface change is
   observed; do not retry an unknown result blindly.
5. Reopen or reread the owner surface after submission. Report success only when
   the final decision/status is visible and the editable submission form is
   closed, locked, or otherwise replaced by a recorded result.

Keep website credentials and session state outside the repository. Do not copy
external-site content into public skill files.

## Draft Approval

`draft create` may create and open an Apple Mail draft. It does not authorize
sending. Let the user review the draft in Apple Mail, run `draft inspect` again
after approval, and pass only the current fingerprint to `draft send`. Any
content change invalidates approval. Never retry an `unknown` send result.

Mailbox delete, archive, move, and mark are outside the current Relay contract.
