---
name: opl-relay
description: Use OPL Relay to inspect configured mailboxes, retrieve approved private context, and prepare, review or explicitly send threaded mail drafts.
---

# OPL Relay

Use the installed Relay CLI as the authority. The plugin carries the runtime
and shared review UI; it never treats its directory as user storage.

## Resolve Runtime

1. Run `opl-relay --json setup status` on first use of a Profile or when its configuration changes. Reuse its resolved account and paths within the task.
2. If the Profile is unconfigured, run `opl-relay --json setup init`.
3. Run `opl-relay --json doctor` once when resolving the runtime, or to diagnose a real configuration failure. Do not repeat setup, account discovery or help calls for each approved mail action.
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
login Keychain. An optional account-level System-keychain fallback is documented
in [the local profile guide](../../../../docs/local-profile.md); credential
rotation must update both copies. Never put a password in a prompt, JSON input, command argument, or
Profile Workspace file. The optional `--connect` check is the first network
operation. When it succeeds, use `sync` and inspect the local evidence.

## Capability Contract

For Hotmail/Outlook.com, use `account add --provider microsoft_graph --client-id
<public-application-id>`, then `account login --account <id>` and the browser's
Microsoft consent. Never request or reuse an IMAP password for a Graph account.
`account folders` inventories the remote tree. `account login --write` enables
the existing exact-reference move contract; it requests no mail-send permission.
Tokens remain in Keychain; Graph uses the same Relay store, review queue and
active/history scopes. Background OAuth renewal is automatic. Server-draft
commands support Graph and IMAP; users may review on their synchronized phone.
See [Microsoft setup](../../../../docs/local-profile.md#hotmail-and-outlookcom).

Routine operations reuse configured credentials and renew OAuth automatically.
Do not request passwords or run `credential set`/`account login` before each
review, draft or move. Background OAuth access never opens Keychain dialogs;
IMAP accounts with an explicit System-keychain copy read that copy directly.
On a real credential failure, stop the affected operation and report one
actionable account error. Retry only after credentials or authorization change.
Interactive credential setup is separate from normal unattended mail work.

Relay owns shared mail-management records and execution checks, not a parallel
mail client. Himalaya is the sole backend for IMAP transport, MIME reads, draft
creation and mail writes. There is no built-in transport fallback. Keep its credentials and account config behind Relay's adapter; do
not bypass exact references, approval or readback by issuing native tool writes.
See [backend setup](../../../../docs/local-profile.md#mature-mail-backend).

## Stable Capabilities

The OPL Package exports four stable capability contracts:

- `communications.mail.v1`: local mailbox evidence, sync, retrieval, controlled
  Archive/Trash/Bill movement, and review-gated Apple Mail drafts;
- `personal.context.v1`: bounded drafting context assembled from evidence;
- `personal.memory.v1`: proposed and explicitly approved relationship memory;
- `knowledge.obsidian.v1`: read-only retrieval from Framework-authorized
  registered knowledge sources, including Obsidian, Profile and project Markdown.

These identifiers describe available contracts. They do not move private data
into the plugin or authorize a mailbox write by themselves.

## Mail And Context

### Shared review workbench

When the user wants visual mail review or an editable draft, use the bundled
`open_workbench` MCP tool. It can focus `account_id`, `storage_ref`, `draft_id`
or `request_id`. It negotiates the host's App support: `presentation.mode` is
`mcp-app` for inline rendering or `browser` with the same workbench's local URL.
Open a returned browser URL in the host's browser panel or on the same machine;
do not repeatedly try an unavailable renderer. If the tool is absent, run
`opl-relay --json workbench open` with the same focus flags. Python 3.11+ and
Node.js 20+ are required. Keep the URL's capability private.

For a host connection, use `workbench connection --host generic|codex|opl` and
the host's native MCP configuration. The Codex Plugin already registers that
server; avoid duplicate manual entries. For official DSH, generate a native
configuration-only bundle with `workbench connection --host dsh --output
<empty-private-directory>` and install it through DSH's `plugin_manager`.
Never copy the engine, mail data or credentials into a second host-owned store.
App-only send and attachment tools are unavailable to ordinary MCP clients;
use the returned browser workbench for human review and confirmation.

Prepare an exact-source local reply using `relay_reply`, or pass the same fixed
action as JSON on stdin to `opl-relay --json workbench action`:

```json
{"action":"reply","input":{"account_id":"work","storage_ref":"email-store://...","request_id":"stable-reply-id","body":"Approved reply text"}}
```

Then open with `--account work --draft-id stable-reply-id`. Reply All derives
To/Cc and quoted history from the exact source; preserve the approved signature
from `templates/mail-signature.txt`. The workbench lets the user edit only the
new reply text and add attachments. Local save does not create a server draft;
publishing does, and sending needs a separate explicit confirmation. Preserve
concurrent edits using the returned revision; never replace input after a
conflict. Published drafts are inspected, not overwritten. An unknown save or
send is reconciled by its original request, never recreated or resent. Continue
to use context and approved memory before drafting; the UI does not replace
that reasoning or authorize mailbox changes.

### Evidence and context

For a read-only People or memory-evidence view, use the Package's
`app-contribution` ABI with operation `read` and ref
`personal.memory.v1#people` or the existing `personal.memory.v1#search`.
People accepts optional `query` and `limit` (1..500, default 50); memory search
also accepts `entity`. Both return a ready data envelope with bounded `items`,
`count` and `source_policy`. People reuses MemoryStore entity identities,
names, aliases and kinds with approved-memory counts, summaries and stable
source references. It does not create a contact database or copy mail. Missing
storage returns an empty result without creating a Profile or database; do not
run `setup init` just to read these views.

Default projections include only approved, currently usable memory. Candidates,
historical events, expired claims and unlinked personal assertions remain for
explicit review through `memory candidates`, `memory inspect` and
`memory search --include-stale`. Inspect full evidence before judging a record;
a truncated summary is not the source. Evidence currentness and claim freshness
are separate: a retained email does not make an old role or commitment current. Collection `command_inputs[action_ref]`
supplies action-derived `input_schema` and empty `defaults`; item `actions`
binds declared actions to exact owner inputs, never approval. The descriptor's explicit
`app_contributions.ui[]` entries let Framework mount People (`list_detail`)
and memory evidence (`timeline`) in `settings.section`; hosts must not infer
placements from navigation or hardcode Relay-specific UI.

Inbox and Drafts also expose `data.items` for generic collection rendering while
preserving legacy mail/snapshot fields. Default Drafts reads only existing ledger
metadata; inspect a selected `draft_ref` through the owner before reviewing its
current content. Never copy a cached fingerprint into send approval automatically.
Unknown or already claimed drafts must not be reopened or resent. The ABI Reply
All action requires the complete native Apple Mail account/id/mailbox tuple, not
a converted `email-store://` identity. A triage view without explicit source and
policy references returns `input_required` rather than ready evidence.

Use `accounts` as account truth. Sync explicitly when freshness matters, then
use `recent`, `search`, and `read` through stable `email-store://` references.
`sync` defaults to active scope. For requested history missing from the cache,
use `search --remote --account <id> --scope history <query>` instead of fetching
an entire archive. Remote results retain the same references, report bounded
coverage, and do not assert a complete folder snapshot or mark mail reviewed.
Before drafting for a known person or project, run `context build`. Use only
approved memories as active relationship memory and re-read raw mail for
high-risk dates, roles, commitments, or invitations.

Default `recent`/`search` scope is current Inbox/Sent. Use `--scope history`
only for requested historical correspondence; it excludes Junk/Trash. A named
`context build --person/--project` uses history. `--folder` explicitly selects
one current folder by name or slug. Run `index` once for older stores; indexed
search covers the entire selected scope without a recency scan cap.

For routine review, `review prepare --account <id>` combines active sync,
snapshot freshness, pending mail, and open items. Omit the account to prepare
all configured accounts. A partial sync can still return fresh available mail
when `available_for_review=true`; retain the explicit remaining coverage gap.
False means the connection or snapshot failed, so the queue is stale context.
Use `review pending --account <id>` for subsequent batches without another sync.
After actually classifying each returned message, write a JSON array containing
`storage_ref`, `action`, `status` (`open`, `waiting`, `closed`, or `none` for no
tracked task), and `note`, with optional `category` and `suggested_folder`,
then run `review record --file <path>`. `review status --account <id>` returns
remaining unreviewed mail and open items still in Inbox. A review record is a
local judgment receipt, never permission to mutate the mailbox. Do not replace
identity-specific progress with a Date-header watermark or mark unexamined
messages reviewed. Archive is historical evidence, not a source of reminders.
Keep a topic category separate from an outstanding action. A suggested folder
does not move mail; use only existing account-specific folders, and preserve
unresolved matters in Inbox. History also includes Archive/Archives descendants
and Bill collections when they have been synchronized.

Sync retries missing UIDs, reports incomplete fetches, and reconciles external
moves without deleting evidence. `read` preserves old references and reports
`present=false` for historical locations; locate a fresh current reference
before proposing a mailbox operation. Snapshot counts are timestamped, not
live counts. A sync failure cannot justify reporting "no new mail".

## Shared Knowledge And Promotion

Use `sources discover` to find unreviewed documents within Framework's granted
source scope, include/exclude paths and size limits. These results are explicitly
`candidate=true`, `accepted=false`; inspect them for review rather than injecting
them as accepted current context. Discovery does not require blanket acceptance
of an existing library.

Read accepted knowledge with `sources list` and `sources search`. Framework
owns source grants, accepted revisions and validity; `sources.toml` no longer
controls access. `sources index` only rebuilds metadata and removes the old
derived document cache. It is optional for search and cannot accept source edits.

Use the global `--knowledge-project /absolute/project/root` when knowledge is
project-scoped. `context build --project <name>` is a search selector and does
not supply that authorization scope. Keep one body in its registered vault,
Profile or project location rather than copying it into Relay storage.

Relay memory records preserve source evidence and canonical knowledge links.
Use `memory export --output <private-json>` for migration/review, and
`memory promote <memory-ref> --knowledge-id <id> --output <private-json>` to
prepare a proposal-only handoff. Persona judges whether the claim is a current
state, historical event, policy or other reusable knowledge. Framework performs
accepted `propose → review → apply` writes. Then `memory link <memory-ref>
--knowledge-id <id>` verifies the accepted source and records its canonical
reference. These commands do not send mail or delete evidence; Relay never
writes a knowledge body itself.

## Controlled Mailbox Movement

Mailbox movement is a separate, explicitly authorized operation. Preserve the
user's keep/watch/uncertain boundary and prepare the exact `email-store://`
references before invoking it. The command defaults to a read-only live
preflight; only `--apply` permits a remote change:

```bash
opl-relay --json mailbox move \
  --account work \
  --destination archive \
  --storage-ref 'email-store://...' \
  --apply
```

Relay resolves an existing Archive, Trash, or Bill folder, or an explicitly
named existing folder via `--destination-folder`. For each reference it checks
the source UID, complete raw-message SHA-256, and `Message-ID`, then verifies
the target and source absence and records a `mailbox-operation://` receipt.
Gmail archive uses UID MOVE from Inbox to the advertised All Mail folder after
confirming the identical message is already there; `-X-GM-LABELS` is not a
verified archive path. Other moves prefer UID MOVE and use UIDPLUS COPY plus
UID EXPUNGE only as a bounded fallback. Never retry an unknown outcome before
read-only reconciliation. Relay never issues an unscoped EXPUNGE, creates
folders, permanently deletes mail, or continues after an uncertain result.
Run a fresh account sync and readback after an applied batch.

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

The local screening route itself never creates, sends, deletes, archives,
moves, marks, or otherwise changes mail. A later, explicitly requested draft
operation remains a separate review-gated write.

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

Use server Drafts for normal replies and revisions; use `server-create` only
for new correspondence. Mail servers own the saved draft. Apple Mail supplies
the existing fingerprint-gated delivery adapter when Codex is asked to send;
server drafting does not require Apple Mail. For desktop-local composition,
manual attachments or UI diagnosis, read [Apple Mail drafts](references/apple-mail.md).

### Routine Reply And Follow-up

1. Reuse the resolved Profile/account. Sync the selected account once when
   freshness matters. For a reminder after the user's unanswered
   outgoing mail, add `--latest-sent`; this selects a newer own Sent message
   sharing actual thread identifiers, never subject alone. Use `draft
   reply-context --account <id> --query <subject> --latest-sent --sync` to do
   selection and fresh sync in one call, or pass an exact `--storage-ref` for
   an ordinary answer. Check coverage and the selected source before composing.
2. Use the thread and already approved personal context to cover every pending
   request, including attachments, travel information or speaker material.
   Preserve all Reply All participants and the quoted chain. New prose and
   the approved signature are separate inputs.
3. Run `draft server-reply-all --account <id> --storage-ref <selected-ref>
   --request-id <stable-id> --body-file <private-file> --signature-file
   <private-file> --apply`. Its verified result already contains complete
   headers, body and a content fingerprint. One successful readback is enough;
   do not immediately repeat `server-inspect`. Use a local preview first only
   when wording or routing still needs review. A prepared, unsaved preview can
   be revised under the same request ID; an existing server draft is never
   overwritten. Use a new request ID for a different source message.
4. Stop at the requested review surface. Saving a draft never grants permission
   to send. If the user approves sending this exact content, call `draft
   server-send --account <id> --request-id <stable-id> --approval <reviewed-fingerprint>`.
   It verifies the live server copy, finds the same Message-ID in Apple Mail,
   compares MIME, routing, body, attachments and thread identifiers, and reuses
   Relay's single-send ledger. It does not require UI search or numeric Mail
   IDs. Apple Mail must have synchronized the draft. If the user has edited it
   or added attachments, inspect and review that current Apple Mail draft
   instead; never silently send the earlier server copy.
5. The send result includes the Sent receipt and scoped leftover-draft cleanup.
   A verified Sent result completes sending even if cleanup is pending; never
   send again to repair cleanup. Use `server-inspect` after an unknown result.
   It reconciles the same handoff and never re-enables an already claimed send.

For independent user sending or pending cleanup, run `draft server-reconcile
--account <id> --request-id <stable-id> --sent-message-id <sent-id>` to preview
only this request and live Sent identity. Add `--apply` when the exact cleanup
is authorized. Keep unknown or edited drafts. A terminal absent/cleaned record
is not scanned again. Leave other drafts alone; routine completion is not an
account-wide housekeeping request. `read <ref>` already includes structured
From/To/Cc, Message-ID, In-Reply-To and References, so another raw MIME dump is
usually unnecessary.

A `server-send --check` run checks delivery readiness without sending or claiming
it. Use only when diagnosing the handoff, not as an extra mandatory step for
every message. Do not run help, source searches, all-account sync, tests or Git
maintenance while completing a routine approved email. If a tool returns a
running session, wait on that same session; an empty output is not evidence
that the operation failed. Reconcile unknown external writes read-only before
considering any retry.

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

Permanent mailbox deletion and marking remain outside the Relay contract.
