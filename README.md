<p align="center">
  <img src="assets/branding/opl-relay-logo.png" alt="OPL Relay logo" width="132" />
</p>

<p align="center">
  <a href="./README.md"><strong>English</strong></a> | <a href="./README.zh-CN.md">中文</a>
</p>

<h1 align="center">OPL Relay</h1>

<p align="center"><strong>A private, evidence-backed communication layer for Codex and One Person Lab</strong></p>
<p align="center">Mail evidence · Relationship context · Approved memory · Obsidian context · Apple Mail review</p>

OPL Relay helps Codex work with your academic and professional email without
turning a source checkout or plugin cache into a personal-data folder. It keeps
the original messages as evidence, builds reusable relationship context, and
routes drafts through Apple Mail for human review before any send.

OPL Relay is a standalone communication product. It can be installed and used
on its own, and it collaborates with OPL Persona through explicit, reviewed
handoffs.

Relay has one public product, Package, Plugin, Skill, and CLI identity:
`opl-relay`. The internal Python module name is not another user-facing mail
entry point.

## What You Can Ask It To Do

- "Review the last three days of mail and tell me what needs a decision."
- "Find my previous exchanges with this person and draft a reply in the same
  relationship context."
- "Create the reply in Apple Mail. Let me review it there, and do not send it."
- "Show the evidence behind this relationship memory before I approve it."
- "Use the approved Persona proposal to create a mail draft, without sending."

## Core Capabilities

**Evidence-first mail**

Relay synchronizes configured IMAP folders into a private local SQLite store.
Search results and context use stable `email-store://` references so a draft can
be traced back to the source message.

**Relationship context with approval**

Long-lived memory is proposed with source evidence. It becomes active drafting
context only after explicit approval.

**Read-only Obsidian context**

Selected Markdown paths can be indexed as a read-only source. Relay never
writes back to the vault.

**Apple Mail review**

Relay creates a real Apple Mail draft, reads it back, and binds approval to its
current fingerprint. Editing the draft invalidates an earlier approval.

**Persona handoff**

OPL Persona can hand an approved, evidence-linked mail context to Relay. Relay
still owns the account, recipients, Apple Mail draft, and separate send gate.

## How The Pieces Fit

| Surface | What it contains | Who manages it |
| --- | --- | --- |
| Git repository | Source code, tests, Plugin files, Skills, and Package descriptor | Git / maintainers |
| Codex Plugin snapshot | Installed communication instructions and carrier metadata | Codex |
| Relay engine | The `opl-relay` CLI and local mail implementation | Embedded in the Codex Plugin native carrier; Relay keeps mail semantics and the carrier keeps physical installed state |
| Profile Workspace | Mail databases, account references, approved memory, policies, and Persona state | The user |

The selected Profile Workspace is the only user-data root:

```text
~/OPL/profiles/<profile>/
  profile/
  policies/
  context/
  templates/
  exports/
  data/
    relay/
    persona/
```

Relay always uses `<profile>/data/relay`. Reinstalling or updating code must not
move, replace, or publish that directory.

## Install The Codex Plugin From GitHub

The public repository can be added as a Git-backed Codex Marketplace:

```bash
codex plugin marketplace add gaofeng21cn/opl-relay --ref main --json
codex plugin list --marketplace opl-relay --available --json
codex plugin add opl-relay@opl-relay --json
codex plugin list --marketplace opl-relay --json
```

To refresh the Git marketplace and reinstall the current Plugin snapshot:

```bash
codex plugin marketplace upgrade opl-relay --json
codex plugin remove opl-relay@opl-relay --json
codex plugin add opl-relay@opl-relay --json
```

Start a new Codex task after installation so the new Plugin snapshot is loaded.

> **Distribution boundary:** this command installs the Codex Plugin directly
> from GitHub. OPL App uses the separate GHCR Package channel described below;
> both deliver the same public Relay carrier but have independent lifecycle
> authorities.

## Start With A New Profile

Requirements: macOS, Apple Mail for draft review, and an IMAP account.

```bash
export OPL_PROFILE_WORKSPACE="$HOME/OPL/profiles/my-profile"
opl-relay --json setup init
opl-relay --json account add \
  --id work --email you@example.com --host imap.example.com
opl-relay --json credential set --account work
opl-relay --json account check --account work --connect
```

`setup init` is idempotent and creates only missing Profile templates and empty
configuration files. `account add` writes IMAP metadata only. The password
prompt is separate and stores the secret in macOS Keychain, never in the
conversation, command arguments, Profile Workspace, or Git.

The local-source developer path remains available:

```bash
git clone https://github.com/gaofeng21cn/opl-relay.git
cd opl-relay
make install-local
```

## A Typical Workflow

```bash
opl-relay --json accounts
opl-relay --json sync --account work --mode incremental
opl-relay --json recent --account work --limit 20
opl-relay --json search "project or person" --account work
opl-relay --json read 'email-store://...'
opl-relay --json context build --person "Professor Example" --query "invitation"
```

Create and inspect an Apple Mail draft:

```bash
opl-relay --json draft create \
  --account work \
  --to 'Recipient <recipient@example.test>' \
  --subject 'Subject' \
  --body-file ./draft.txt

opl-relay --json draft inspect 'mail-draft://apple-mail/work/UUID'
opl-relay --json draft open 'mail-draft://apple-mail/work/UUID'
```

Reply to an existing multi-recipient Apple Mail message by using Mail's native
Reply All route to derive recipients and subject, then materializing that route
as a stable review draft. Use the exact `account`, numeric message `id`, and
`mailboxPath` returned by the Apple Mail local screen:

```bash
opl-relay --json draft reply-all \
  --account work \
  --apple-mail-account 'Work' \
  --apple-mail-id 12345 \
  --mailbox-path 'INBOX/Conference' \
  --body-file ./reply.txt
```

Relay does not reconstruct recipients from a flattened mail record. It rejects
self-addresses, duplicates, Bcc, empty recipient sets, and ambiguous source
tuples before registering the stable review draft. The saved draft is the
review surface; provider-native Reply All is the routing authority.

Sending is a separate, explicit action. Inspect the current draft after review
and use only the fingerprint returned by that readback. Any content change
invalidates the earlier approval, and an unknown send result is never retried
automatically.

## OPL App Distribution Status

Relay declares an OPL Capability Package, while publication and physical
installation remain separate authority surfaces:

```text
Relay owner descriptor + immutable GHCR publication
  -> OPL Base download / verify / bytes handoff
  -> configured native carrier install / update / repair / uninstall
  -> native installed readback
  -> Framework discovery / carrier delegation / aggregation
  -> OPL App generic Package projection
```

The contracted stable Package source is
`ghcr.io/gaofeng21cn/one-person-lab-packages/opl-relay:latest-stable`.
Package publication must be read from the public immutable GHCR digest.
Installed/current state must be read separately from the configured native
carrier; Framework only delegates actions and aggregates that readback for App.
GitHub remains the source and Codex Plugin Marketplace input; GitHub Releases
are not part of Relay distribution. See
[Distribution](docs/distribution.md) for the authority and availability
boundaries.

## Safety Boundary

- User mail, account configuration, SQLite files, raw EML, sync cursors,
  private policies, Obsidian paths, and credentials never belong in Git or a
  Plugin cache.
- Relay is read-first. Delete, archive, move, and mark are not exposed.
- A Persona approval does not authorize mail sending.
- Apple Mail draft review and fingerprint-bound send approval remain separate.

## Documentation

- [Distribution and update model](docs/distribution.md)
- [Architecture](docs/architecture.md)
- [Profile Workspace](docs/local-profile.md)
- [Runtime contract](docs/workspace-contract.md)
- [Relay, Persona, and OPL App](docs/product-architecture.md)

## Development

```bash
python3 -m pip install -e . pytest
make test
make validate-package
```

The repository CI also validates the Plugin structure and exercises discovery
and installation through an isolated local Codex Marketplace.

## License

[Apache License 2.0](LICENSE)
