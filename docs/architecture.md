# Architecture

Codex Mail Workbench separates reusable mail tooling from private local state.
The public repository should describe and ship the generic workbench. A user's
real mail accounts, local database, sync cursors, and personal operating notes
stay outside the public source tree, normally in `./local` during development or
in another directory selected with `CODEX_MAIL_HOME`.

## Layers

1. Configuration layer: account metadata from `accounts.yaml`, with secrets
   resolved from macOS Keychain service `codex-mail-workbench`.
2. Protocol layer: IMAP sync using the configured account endpoints and folder
   include/exclude rules.
3. Store layer: SQLite table `email_messages`, keyed by
   `(account_id, folder_slug, uid)`, with raw EML retained locally.
4. Draft layer: Apple Mail stores the editable draft while a separate private
   SQLite ledger stores only stable identity, state, fingerprints, and receipts.
5. CLI layer: read commands plus `draft create/adopt/inspect/open/send`.
6. MCP layer: read-only `mail_recent`, `mail_search`, and `mail_read`.
7. Skill layer: Codex uses the CLI first and the MCP server when the host app
   configures it.

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

## Public and Private Boundary

Public repository contents:

- reusable Python package under `src/`;
- tests for reusable behavior;
- documentation and generic examples;
- Codex skill instructions that use placeholder account ids such as `work` and
  `personal`.

Private local profile contents:

- `accounts.yaml` with real account ids, email addresses, IMAP/SMTP hosts,
  folder filters, usernames, and Keychain secret references;
- `profile.md` with the user's mailbox triage preferences or response style;
- `sync-state/` JSON files with per-account IMAP sync cursors;
- `mail.sqlite`, `mail.sqlite-shm`, and `mail.sqlite-wal` with synced message
  metadata and raw EML content.
- `drafts.sqlite`, `drafts.sqlite-shm`, and `drafts.sqlite-wal` with private
  draft lifecycle metadata and receipts.

The normal default state directory is `~/.codex-mail-workbench`. For a repo-local
private profile, run commands with `CODEX_MAIL_HOME=./local`. The repository
contract is that `local/` is private working state and must not be published.

## Write Boundary

The implemented write path is limited to Apple Mail drafts:

- `draft create`
- `draft adopt`
- `draft inspect`
- `draft open`
- `draft send --approval <fingerprint>`

MCP remains read-only. Future `mark`, `move`, `archive`, or delete operations
require separate contracts and authorization.

Live `send` requires an explicit user request, rejects any post-review edit,
executes at most once, and records a local Sent receipt.
