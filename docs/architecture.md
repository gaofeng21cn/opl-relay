# Architecture

Codex Mail Workbench separates reusable mail tooling from private local state.
The public repository should describe and ship the generic workbench. A user's
real mail accounts, local database, sync cursors, and personal operating notes
stay outside the public source tree, normally in `./local` during development or
in another directory selected with `CODEX_MAIL_HOME`.

## Layers

1. Configuration layer: account metadata from `accounts.toml`, with secrets
   resolved from macOS Keychain service `codex-mail-workbench`.
2. Protocol layer: IMAP sync using the configured account endpoints and folder
   include/exclude rules.
3. Store layer: SQLite table `email_messages`, keyed by
   `(account_id, folder_slug, uid)`, with raw EML retained locally.
4. Draft layer: Apple Mail stores the editable draft while a separate private
   SQLite ledger stores only stable identity, state, fingerprints, and receipts.
5. Memory layer: `memory.sqlite` stores entities, evidence-backed memories, and
   explicit candidate/approved/superseded/rejected/forgotten states.
6. Knowledge layer: configured read-only providers index private source
   documents; Phase 1 ships an Obsidian Markdown provider.
7. Context layer: a bounded package combines approved memory, selected raw-mail
   evidence, and indexed knowledge excerpts for one drafting task.
8. CLI layer: mail reads, local memory/knowledge management, context building,
   and `draft create/adopt/inspect/open/send`.
9. Skill layer: Codex uses the CLI as the single Workbench interface.

## Store Reference

Messages are addressed as:

```text
email-store://<account_id>/<folder_slug>/<uid>/<raw_sha256_prefix>
```

This is stable for local reads and safer than Mail.app numeric ids.

## Draft Reference And Approval

Apple Mail drafts are addressed as:

```text
mail-draft://apple-mail/<account_id>/<X-Universally-Unique-Identifier>
```

The Apple `X-Universally-Unique-Identifier` remains stable when Mail changes its
numeric id or Message-ID during saves. The local `drafts.sqlite` ledger stores
that identity, the lifecycle state, content fingerprints, and Sent receipts. It
does not store recipients, subjects, bodies, or attachment bytes.

`draft inspect` fingerprints the account, Apple Mail account, sender, ordered
To/Cc/Bcc lists, subject, normalized body, and attachment metadata/content
hashes. `draft send` accepts only that exact fingerprint. The ledger atomically
changes `draft` to `sending` before invoking Mail, so concurrent or repeated
sends fail closed. A transport or automation ambiguity becomes `unknown` and is
never retried automatically. Only Sent-mailbox readback produces `sent`.
`draft adopt`, `draft inspect`, and `draft send` reconcile Sent by the Apple
UUID before treating an object as sendable, including when the Draft no longer
exists.

Apple Mail account signatures are disabled for Workbench-created drafts. The
reviewed input body is the complete message body, preventing account-local
signature changes from bypassing approval.

## Memory Reference And Evidence

Entities and memories are addressed as:

```text
mail-memory://entity/<uuid>
mail-memory://fact/<uuid>
```

Every memory requires one or more source references. Email-derived memory uses
the original `email-store://...` reference and raw SHA-256 as provenance.
Candidate memories never appear in drafting context. Approval is an explicit
CLI transition; replacement facts point to the earlier memory and mark it
`superseded`. Reject and forget retain provenance instead of silently deleting
history.

Codex performs open-ended interpretation and proposes structured candidates.
The Workbench enforces identity, evidence, deduplication, lifecycle, and
retrieval boundaries. It does not claim that a generated inference is an
observed fact.

## Knowledge Providers And Context

`sources.toml` preconfigures provider roots. The Obsidian provider reads
Markdown and writes only a derived index to the private `memory.sqlite`; it
never edits the vault. `context build` retrieves a bounded number of approved
memories, relevant mail messages, and knowledge excerpts. Mail and knowledge
content are untrusted data, never executable agent instructions.

## Public and Private Boundary

Public repository contents:

- reusable Python package under `src/`;
- tests for reusable behavior;
- documentation and generic examples;
- Codex skill instructions that use placeholder account ids such as `work` and
  `personal`.

Private local profile contents:

- `accounts.toml` with real account ids, email addresses, IMAP hosts, folder
  filters, usernames, and Keychain credential references;
- `profile.md` with the user's mailbox triage preferences or response style;
- `sync-state/` JSON files with per-account IMAP sync cursors;
- `mail.sqlite`, `mail.sqlite-shm`, and `mail.sqlite-wal` with synced message
  metadata and raw EML content.
- `drafts.sqlite`, `drafts.sqlite-shm`, and `drafts.sqlite-wal` with private
  draft lifecycle metadata and receipts.
- `memory.sqlite`, `memory.sqlite-shm`, and `memory.sqlite-wal` with identities,
  memories, evidence, and derived knowledge content.
- `sources.toml` with private provider paths and filters.

The normal default state directory is `~/.codex-mail-workbench`. For a repo-local
private profile, run commands with `CODEX_MAIL_HOME=./local`. The repository
contract is that `local/` is private working state and must not be published.

## Write Boundary

Local-only CLI writes include entity and memory lifecycle changes plus rebuildable
knowledge indexing. These writes do not change the mailbox or Obsidian vault.

The externally visible write path is limited to Apple Mail drafts:

- `draft create`
- `draft adopt`
- `draft inspect`
- `draft open`
- `draft send --approval <fingerprint>`

Future `mark`, `move`, `archive`, or delete operations require separate
contracts and authorization.

Live `send` requires an explicit user request, rejects any post-review edit,
executes at most once, and records a local Sent receipt.
