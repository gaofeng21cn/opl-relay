# OPL Relay Product Integration

Owner: `opl-relay`
Purpose: `relay_cross_repo_product_boundary`
State: `active_current`

This document owns Relay's handoffs to Persona and App. Relay's internal
implementation and runtime workspace invariants belong to
[Architecture](architecture.md), and physical
delivery to [Distribution](distribution.md). Other repositories own their
actual implementation and operational state.

## Product And Owners

Relay is the digital delegate's shared mail-management layer: it turns
communication into continuous, verifiable work records across OpenClaw, Codex
and OPL. It retains review progress, reasons, thread/task relationships and
execution receipts. It is not intended to replace a general mail client.
Persona combines cross-domain PI context and produces proposals; it does not own Relay's mail store.
OPL App supplies the chat and visual product. Relay owns its shared mail-review
view within this repository; Codex and other hosts render it through MCP Apps
or a browser panel. There is no separate Relay UI repository or Persona desktop product.

| Boundary | Owner |
| --- | --- |
| Original messages, actual folder/label membership, server drafts and Sent status | Mailbox provider |
| Replaceable mail protocol, retrieval and write implementation | Mature mail tools where their actual contracts cover the operation |
| Stable mail identities, raw evidence, relationship memory, draft fingerprints and send receipts | Relay |
| Reasoning, drafting and user interaction | OpenClaw, Codex or OPL using the same Profile and Relay records |
| Cross-domain context, private policy interpretation, provenance and proposals | Persona |
| Canonical knowledge bodies and personal profile values | User-owned registered vault, Profile or project source |
| Knowledge registry, grants, accepted revisions and source writeback | Framework |
| Public content, approved local website changes and publication | `gflab_web` |
| Runtime, Package discovery, carrier delegation and App projection | Framework |
| App product contracts and acceptance | `one-person-lab-app` |
| Desktop, WebUI and Docker rendering | `opl-studio` |
| DSH/Cordis Application Host, native Codex and delivery composition | `opl-studio` |

App selects the current implementation through its
[Shell adapter](https://github.com/gaofeng21cn/one-person-lab-app/blob/main/contracts/app-shell-adapter.json).
[OPL DSH](https://github.com/gaofeng21cn/opl-dsh) is the independent enhancement
suite for the official DeepSeek Harness desktop.

The target relationship is described in
[Persona Architecture Guidance](https://github.com/gaofeng21cn/opl-persona/blob/main/docs/architecture-guidance.md).
It does not override the current contracts of App, Framework, a carrier or a
domain provider. A host never absorbs domain identities or private state.

## Persona Handoff

Relay's `triage evidence` returns an evidence-only
`opl-relay-mail-triage-evidence.v2` envelope for one synced
`email-store://` reference. It includes original-message readback, parsed
recipient facts, freshness and policy references. Its `policy_digest` hashes
the ordered reference set; it does not read or hash Persona's private Markdown.

The envelope requires human review and forbids external writes. Relay
`triage validate` validates identity, provenance, reference-set digest and
the read-only boundary. It accepts the bare envelope or the exact successful
JSON wrapper emitted by `triage evidence`:

```bash
opl-relay --json triage evidence 'email-store://...' --policy-ref 'policy://...' \
  | opl-relay --json triage validate --input -
```

Persona owns interpretation, policy-content digests and Inbox staging.
For relationship memory, Persona consumes Relay's fresh public
`personal.memory.v1#people` and `personal.memory.v1#search` reads. Relay checks
source currentness and claim freshness before returning approved summaries;
the consumer does not read Relay's SQLite files or promote an old result to a
personal fact store. Relay exports a proposal-only memory promotion with its
original evidence; Persona judges its meaning and destination, Framework applies
accepted source writes, and Relay links the result by canonical knowledge ID.
User knowledge retains one body at its registered source. The same Framework
registry governs Relay's read scope and revisions. See the
[currentness boundary](architecture.md#knowledge-and-memory-currentness).

Relay validates a separately approved Persona `mail.draft_context` proposal
before creating an Apple Mail review draft. Persona approval never authorizes
a send. Relay still owns the account and recipient route, draft identity,
post-review fingerprint and authoritative send result.

## App Contributions

The carrier-root `plugins/opl-relay/opl-package.json` declares the current
role-neutral `app_contributions` and `app-contribution` CLI ABI. App consumes
structured data and opaque actions through Framework; it must not special-case
Relay identity, require a `standard_agent` role or embed Package UI code.

Framework owns the Host within runtime, Package graph and App projection.
Studio's separate DSH Host composes profile/plugin/executor and delivery
transport; public App state/action, authentication and channel callbacks join
the scopes without sharing registries, sessions or currentness. Relay remains
a Python capability behind its declared ABI and introduces no Host or second
lifecycle manager.

Current declared views and callable refs come from the descriptor and
`cli.py`, not a second hand-maintained UI inventory here. Their presence does
not prove App rendering, native-carrier installation, healthy accounts or a
completed external write. A host can show unavailable contributions locally
without blocking unrelated Packages.

The shared workbench, Apple Mail, and synchronized mail clients are review
surfaces over Relay's existing draft contracts without merging Persona proposal
approval and Relay sending authority. Inbox and Drafts expose `workbench.open`
via App's action bridge and MCP Apps / browser handoff (with official DSH receiving
a configuration-only bundle); installation and rendering belong to hosts while
mail state and interaction details remain in
[Shared Mail Workbench](architecture.md#shared-mail-workbench) and the
[workbench specification](../workbench/SPEC.md).
