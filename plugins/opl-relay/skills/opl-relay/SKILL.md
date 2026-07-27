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

## Mail And Context

Use `accounts` as account truth. Sync explicitly when freshness matters, then
use `recent`, `search`, and `read` through stable `email-store://` references.
Before drafting for a known person or project, run `context build`. Use only
approved memories as active relationship memory and re-read raw mail for
high-risk dates, roles, commitments, or invitations.

## Draft Approval

`draft create` may create and open an Apple Mail draft. It does not authorize
sending. Let the user review the draft in Apple Mail, run `draft inspect` again
after approval, and pass only the current fingerprint to `draft send`. Any
content change invalidates approval. Never retry an `unknown` send result.

Mailbox delete, archive, move, and mark are outside the current Relay contract.
