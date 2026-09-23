# OPL Relay Architecture

Owner: `opl-relay`
Purpose: `relay_implementation_architecture`
State: `active_current`
Machine boundary: This document explains Relay-owned source and data boundaries. Package publication, configured-carrier installation, Framework aggregation, App rendering, and live runtime state remain authoritative only in their owning descriptors, repositories, carrier readback, and runtime output.

Cross-repository handoffs belong to [Product Integration](product-architecture.md).
This document owns only the Relay implementation view.

OPL Relay is the mail capability module in the planned personal digital
delegate stack. It is local-first, evidence-backed, and useful independently of
OPL Persona or OPL App.

## Layers

1. The engine owns IMAP sync, raw EML storage, stable identities, structured
   memory, knowledge indexing, context assembly, and draft approval semantics.
2. The Codex Plugin supplies the `opl-relay` Skill. It is an installable
   capability carrier, not a database owner.
3. The owner Package descriptor exposes the same capability and its current
   role-neutral `app_contributions`; installed availability still requires
   fresh native-carrier readback.
4. OPL App is the long-term user entry and visual management surface. It calls
   Relay through the same runtime boundary as Codex rather than implementing a
   second mail engine.
5. OPL Persona is the cross-domain orchestration product that may call Relay
   together with other domain modules. It does not absorb Relay's mail truth.

## Engine Modules

- Configuration: account metadata and Keychain credential references.
- Protocol: explicit IMAP synchronization with per-folder cursors.
- Store: local SQLite raw EML store with stable `email-store://` identities.
- Memory: evidence-backed candidate and approved relationship memory.
- Knowledge: read-only Obsidian indexing into a rebuildable local index.
- Context: bounded packages combining approved memory, selected mail evidence,
  and relevant knowledge excerpts.
- Drafts: Apple Mail as the editable review UI plus a local lifecycle ledger and
  exact approval fingerprint. An approved Persona `mail.draft_context`
  proposal may prepare this review draft, but it never authorizes sending.
- Triage evidence: a facts-only v2 envelope exposes mail headers, recipient
  routing, and reference-set provenance. Persona owns private Markdown reading,
  content digests, and all triage judgments.
- CLI: `opl-relay` is the only public command entry. Internal Python names and
  credential-store identifiers do not create additional command identities.

## Runtime Ownership

Relay separates replaceable installation bytes from one user-owned Profile
Workspace:

| Surface | Owner | Contents |
| --- | --- | --- |
| Package owner descriptor | Relay | Package identity, capabilities, content lock, role-neutral App contributions, and publication references |
| Installation | Configured native carrier | Replaceable code, manifest, Skills, lifecycle actions, and physical installed-state readback |
| Profile Workspace | User | Profile, policies, context, templates, exports, and Relay state under `data/relay` |

The installation root is replaceable. Removing, repairing, or upgrading a
carrier installation must not delete the user-owned `OPL_PROFILE_WORKSPACE`.
Framework may discover the installed descriptor, delegate an action, and
aggregate the carrier result, but it does not become a second physical
installation owner. Codex App and OPL App must use the same configured Relay
state under that workspace when they act for the same user.

See [Workspace Contract](workspace-contract.md) for the single-root rule and
[Product Architecture](product-architecture.md) for the broader OPL
integration.

## Server Drafts For Mobile Review

`draft server-create` and `draft server-reply-all` are IMAP-only alternatives to
the desktop draft provider. They prepare locally by default; `--apply` appends
only to one advertised Drafts folder with the `\\Draft` flag. There is no SMTP,
send, source-marking, draft-overwrite or draft-delete operation on this route.
The user reviews and sends through their preferred mail client.

Reply All binds the full raw source by `email-store://` and hash. Recipients are
From (or Reply-To), original To, and Cc, excluding all configured own addresses,
explicit verified aliases, and duplicates. Replies to an own sent message retain
the original external recipients. Bcc-bearing, malformed, or ambiguous sources
fail closed. In-Reply-To and References preserve the conversation relationship.
The full readable source body supplies quoted history; HTML-only sources are
converted to safe text without active content. Original attachments are not
automatically forwarded. Explicit attachments retain their names and bytes.

New prose and an approved signature are separate inputs. The builder adds the
signature once above the quote and generates UTF-8 plain text plus escaped HTML
paragraphs. Wording quality remains an agent responsibility. Existing drafts
edited on a phone are never silently overwritten or replaced.

The existing private `drafts.sqlite` holds `server_draft_requests`: one stable
request ID, account, content identity, MIME bytes, target folder and state.
SQLite serializes writers; attempted state is committed before APPEND. A retry
uses the original Message-ID and reconciles by server search and BODY.PEEK[];
an uncertain or disappeared draft is never blindly appended again. The remote
Draft flag, recipients, thread headers, decoded plain/HTML bodies and attachment
content must match before `server_verified=true` is returned. `server-inspect`
performs this readback without creating another draft. A saved server draft does
not prove that a particular phone has completed synchronization or rendered it.

### Leftover draft reconciliation

Mail clients commonly send a review draft as a new message and leave the stored
draft behind. The server keeps the draft, and a client that already downloaded
it keeps a local copy, so the same text can be sent twice by accident.
`draft server-reconcile` compares this account's verified drafts against its Sent
folder and removes the ones a sent reply already covers:

```bash
opl-relay --json draft server-reconcile --account <account-id>          # preview
opl-relay --json draft server-reconcile --account <account-id> --apply  # remove
```

Only ledger-owned rows in state `verified` are considered, so a draft this
engine did not create is never touched. A draft becomes a candidate only when a
sent message shares its reply target (`In-Reply-To`), its complete To/Cc address
set, and its subject, and is not older than the draft. A weaker signal leaves the
draft alone. A server draft whose contents differ from Relay's verified copy is
always kept, preserving edits made in another mail client. The same subject with a
different reply target, a different recipient set, or an earlier sent copy is
reported as `keep`. An already-deleted or otherwise absent draft is reported as
`absent` without a write.

Removal requires UIDPLUS; without it the command fails closed rather than
issuing an unscoped EXPUNGE. The draft is flagged `\Deleted` and removed with a
UID-scoped EXPUNGE, then the Message-ID is searched again; a removal that cannot
be confirmed is an error, not a success. Applied rows move to state `cleaned`, so
a later `server_draft` call with the same request ID still refuses to recreate
the draft. The Sent copy is never modified, and the Apple Mail local copy is a
client cache: after the server draft is gone, remove any remaining local copy
through the Mail.app route rather than by writing IMAP again.

## Stable References

Mail and memory use stable references rather than direct SQLite facts:

```text
email-store://<account_id>/<folder_slug>/<uid>/<raw_sha256_prefix>
mail-memory://entity/<uuid>
mail-memory://fact/<uuid>
mail-draft://apple-mail/<account_id>/<apple-mail-uuid>
```

## Mail Membership, Search, And Review Progress

Raw evidence and current folder membership are separate. `present` projects a
validated IMAP `SELECT` / `SEARCH ALL` snapshot; it is not a remote delete or
read flag. Rows remain readable by `storage_ref` after movement or UIDVALIDITY
changes. New UID generations must not overwrite earlier raw evidence. Mailbox
operations require a current reference and retain their live provider checks.

Immutable MIME bytes live once per SHA-256 in `email_blobs`; `email_messages`
retains every folder identity and joins that content on read. The one-time
in-place migration verifies byte equality before removing duplicate inline
payloads. Back up the store before migration; `VACUUM` can reclaim freed pages
after verification. This storage change does not delete provider messages.

Sync resumes from missing UIDs in the current UIDVALIDITY, not just a high-water
cursor. Newest missing UIDs are fetched first; older failed fetches remain retryable.
Empty folders reconcile to zero only
after a valid matching SELECT/SEARCH response. A partial or failed folder is
reported as incomplete; `status` includes the snapshot timestamp, server count,
and local current count. Sync processes share one local database lock. FETCH
batches are bounded by declared message size; large MIME messages are fetched
in verified 1 MiB chunks so attachments cannot stall an entire batch. Partial
reads are verified against the requested byte count. A full read whose length
disagrees with the advertised `RFC822.SIZE` is accepted only when an independent
re-retrieval returns identical bytes; otherwise it stays a retryable gap.

`recent` and `search` default to `active` (current Inbox and Sent). `history`
adds archived and retained correspondence; known Trash/Junk copies are excluded.
History includes `Archive/` and `Archives/` descendants and `Bill`, `Bill/`,
and `Bill ` collections. Sync still respects configured folder inclusion rules;
a folder's existence does not mean its messages have been downloaded.
`all` is an explicit evidence-inspection scope. An explicit `--folder` selects
that current folder by display name or slug. Results are local evidence, not
proof of server freshness; sync and inspect snapshot completeness first.

The decoded body index is keyed by raw SHA-256, so identical copies are parsed
once. SQLite FTS5 trigram accelerates substring search, including Chinese;
shorter-than-three-character queries scan decoded text. `index` backfills old
stores. Search refuses an incomplete body index rather than claiming no matches.
The retained `--max-scan` option no longer truncates candidate history. Results
deduplicate by account and Message-ID (falling back to hash), preferring a
current location. A result limit bounds output, not the searchable history.

`review pending` selects current Inbox messages without an identity-specific
review receipt, independent of the message's Date header. `review record`
stores supplied judgments and open/waiting/closed states in the private store
(`none` means reviewed evidence without a tracked task, not a resolved matter);
it performs no classification itself and grants no provider write authority.
Optional `category` and `suggested_folder` fields keep the subject area separate
from the next action. A folder suggestion is descriptive, not a move receipt or
a promise that the move API supports that folder. Use `none` for batched archive
or cleanup proposals that should not clutter the actionable reminder queue.
`review status` returns open items only while their evidence is still in Inbox.
Persona or the calling agent owns interpretation and the user's private policy.
Closed items stay recorded, while Archive never creates reminders. All hosts
consume the same review table rather than maintaining competing date ledgers.

`review prepare` combines active sync, pending messages, and open items across
configured accounts in one CLI call. Its default per-folder sync limit is 200;
remaining holes are explicit, not silently skipped. `available_for_review`
distinguishes a freshly validated Inbox snapshot with some missing bodies from
an account whose connection or UID enumeration failed. Only the former provides
a current queue; both keep their completeness gap visible. The caller controls
how many judgments to perform in a run and stores only completed judgments.

## Safety Boundary

Relay remains read-first. Local memory lifecycle and derived knowledge indexing
are private local writes. Apple Mail drafts remain review-gated, and sending
requires the current post-review fingerprint. A separately contracted
`mailbox move` may move exact, freshly verified references to an existing
Archive, Trash, or Bill folder under explicit `--apply`; the destination must
already exist and is resolved by name, with no folder creation. Permanent
delete and mark remain unavailable.
