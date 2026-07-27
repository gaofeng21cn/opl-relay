# OPL Relay

OPL Relay is a local-first personal communication relay for Codex and OPL. It
syncs IMAP mail into a private raw EML SQLite store, maintains evidence-backed
relationship memory, indexes selected Obsidian Markdown as a read-only source,
and manages review-gated Apple Mail drafts.

This repository is the direct successor to Codex Mail Workbench. The Python
module and `codex-mail` command remain compatible while the product, package,
plugin, and repository identity use `opl-relay`.

## Runtime Model

Relay separates three surfaces:

- installation: replaceable code, plugin manifest, and Skills;
- data root: accounts, mail, drafts, memory, sync cursors, and indexes;
- workspace: profile, policies, context, templates, notes, and exports.

Use `OPL_RELAY_HOME` and `OPL_RELAY_WORKSPACE` to select the last two. Existing
`CODEX_MAIL_HOME` and `~/.codex-mail-workbench` installs remain readable.

```bash
make install-local
opl-relay --json doctor
opl-relay --json workspace init
```

The compatibility command is equivalent:

```bash
codex-mail --json doctor
```

## Core Workflow

```bash
opl-relay --json accounts
opl-relay --json sync --account work --mode incremental
opl-relay --json recent --account work --limit 20
opl-relay --json search "project or person" --account work
opl-relay --json read 'email-store://...'
opl-relay --json context build --person "Professor Example" --query "invitation"
```

Relationship memory is proposed with evidence and becomes active only after
explicit approval. Obsidian indexing is read-only.

For Apple Mail drafts:

```bash
opl-relay --json draft create \
  --account work \
  --to 'Recipient <recipient@example.test>' \
  --subject 'Subject' \
  --body-file ./draft.txt
opl-relay --json draft inspect 'mail-draft://apple-mail/work/UUID'
opl-relay --json draft open 'mail-draft://apple-mail/work/UUID'
```

After the user reviews and approves the current draft, inspect again and use
only the returned current fingerprint:

```bash
opl-relay --json draft send \
  'mail-draft://apple-mail/work/UUID' \
  --approval 'sha256:CURRENT_FINGERPRINT'
```

Any content change invalidates approval. An unknown send result is never
automatically retried.

## Plugin

The installable Codex Plugin scaffold is under
[`plugins/opl-relay`](plugins/opl-relay). It ships capability instructions only
and never owns user data. A future OPL Package should expose the same capability
through role-neutral App contributions.

## Privacy And Safety

Never commit real account configuration, SQLite files, raw mail, sync cursors,
relationship memory, Obsidian paths, private policies, or credentials. Secrets
remain in macOS Keychain service `codex-mail-workbench` for upgrade
compatibility.

Relay is read-first. Delete, archive, move, and mark are not exposed. Sending is
limited to the exact review-gated Apple Mail draft lifecycle.

## Documentation

- [Architecture](docs/architecture.md)
- [Data and workspace](docs/local-profile.md)
- [Runtime contract](docs/workspace-contract.md)
- [Relay, Persona, and OPL App](docs/product-architecture.md)

## Verification

```bash
make test
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/opl-relay
```
