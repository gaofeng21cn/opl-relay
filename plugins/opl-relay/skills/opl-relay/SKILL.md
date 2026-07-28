---
name: opl-relay
description: Use OPL Relay to inspect configured mailboxes, retrieve approved private memory and Obsidian context, and manage review-gated Apple Mail drafts.
---

# OPL Relay

Use the installed Relay CLI as the authority. The plugin contains capability
instructions only; it never treats the plugin directory as user storage.

## Resolve Runtime

1. Run `opl-relay --json setup status` before mailbox work.
2. If the Profile is unconfigured, run `opl-relay --json setup init`.
3. Run `opl-relay --json doctor` before reading or syncing mail.
4. Treat `profile_workspace` as the user's single Profile Workspace, Relay's
   `state_dir` as its `data/relay` child, and `workspace.path` as that same
   profile root.
5. Honor `OPL_PROFILE_WORKSPACE` as the only profile root; Relay state is its
   `data/relay` child.
6. Read `<workspace>/AGENTS.md` when it exists, then follow its profile,
   policies, context, skills, and templates references.

Never put accounts, SQLite files, raw mail, sync cursors, memories, private
policies, or credentials in the plugin cache or source checkout. Credentials
remain in macOS Keychain service `codex-mail-workbench`; the service name is a
local credential contract and is never written to Git.

## First use

Use the same `OPL_PROFILE_WORKSPACE` selected by OPL Persona. The engine is
carried by this Plugin, so a fresh Plugin install does not require a source
checkout:

```bash
opl-relay --json setup init
opl-relay --json account add \
  --id work --email you@example.com --host imap.example.com
opl-relay --json credential set --account work
opl-relay --json account check --account work --connect
```

`account add` writes only IMAP metadata. `credential set` reads a password
interactively (or from stdin with `--secret-stdin`) and stores it in macOS
Keychain; never put a password in a prompt, JSON input, command argument, or
Profile Workspace file. The optional `--connect` check is the first network
operation. When it succeeds, use `sync` and inspect the local evidence.

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

## Apple Mail Local Screen

Apple Mail same-day and UI-local inspection is a Relay fact adapter, not a
second user-facing skill. When the request explicitly concerns Mail.app,
read/unread state, or a small same-day screen:

1. Use the Apple Mail adapter's metadata-first route, then read only selected
   messages and necessary thread context.
2. Preserve the exact Apple Mail `id`, `account`, and `mailboxPath` tuple; do
   not translate it into an `email-store://` reference.
3. Hand the selected facts to Persona when private Markdown judgment is needed.
   Persona returns proposals only and never authorizes a mailbox write.
4. Restart fact gathering through Relay's synced CLI route when the request
   requires IMAP freshness, complete inbox coverage, multiple dates, stable
   `email-store://` provenance, or auditable recipient headers.

This adapter never creates, sends, deletes, archives, moves, marks, or
otherwise changes mail.

## Persona Triage Evidence

Use `triage evidence` to provide Persona one facts-only mail envelope. It
contains stable `email-store://` provenance, `From`/`To`/`Cc`/`Bcc` headers,
recipient routing facts, raw-message hashes, freshness, and policy references.
The `policy_digest` hashes only the ordered policy-reference set; it is never a
private Markdown content digest. Persona must read its own Profile Workspace
and calculate any Markdown content digest itself.

```bash
opl-relay --json triage evidence 'email-store://…' \
  --policy-ref 'policy://persona/mail-triage/v1' > relay-evidence.json
```

This evidence cannot classify, prioritize, forward, delete, archive, move, or
mark mail. `triage validate --input` verifies only Relay provenance and the
read-only boundary.

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

## Persona Draft Handoff

When Persona has produced a user-approved `opl-persona-proposal.v1` bundle with
one `mail.draft_context` proposal targeting `opl-relay.draft.context`, Relay can
create the review draft while keeping send authorization separate:

```bash
opl-relay --json persona draft-create \
  --input ./approved-persona-proposal.json \
  --account work \
  --to 'Recipient <recipient@example.test>'
```

The proposal must contain non-empty `source_refs`, `approval.status=approved`,
an `approval_ref`, `approval.required=true`, and
`approval.external_write_allowed=false`. Its payload is limited to
`subject_hint`, `body_context`, and `tags`; account and recipients remain
Relay-owned inputs. This command only saves an Apple Mail draft and returns
`send_allowed=false`. Inspect the returned draft in Apple Mail before any
separate, fingerprint-bound `draft send` operation.

Mailbox delete, archive, move, and mark are outside the current Relay contract.
