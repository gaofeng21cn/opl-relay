# OPL Relay, Persona, And App Product Architecture

The cross-repository design authority is
`opl-persona/docs/architecture-guidance.md` in the sibling `opl-persona`
repository. This document records the Relay-specific consequences of that
guidance.

## Decision

Use two business repos: `opl-relay` owns communication and `opl-persona` owns
cross-domain PI context and proposal orchestration. Keep OPL App as the
long-term user entry rather than building a second Persona-specific macOS shell.

## Product Model

```text
OPL App
  visual mail, memory, knowledge, Persona proposal, and work views
  generic Package contribution host
        |
        v
Codex / OPL app-server runtime
  chat-first reasoning, tools, approvals, session continuity
        |
        +--> OPL Persona Plugin / Package
        |
        +--> OPL Relay Plugin / Package
               |
               v
             Relay engine
               |
               +--> user data root
               +--> active workspace
               +--> Apple Mail review UI
               +--> Obsidian read-only source
```

## Repository Boundaries

| Repository | Keep/Create | Responsibility |
| --- | --- | --- |
| `opl-relay` | Renamed from the Codex Mail Workbench repo; current mail repository | Mail engine, CLI, Relay Skill/plugin, Package adapter, and mail-context bridge |
| `opl-persona` | Current cross-domain repository | PI context, provenance, review-gated proposals, and cross-domain Skills |
| `one-person-lab` | Keep | Generic Package/runtime/workspace contracts |
| `one-person-lab-app` | Keep | App product shell and role-neutral contribution UX |
| `opl-aion-shell` | Keep | Desktop shell projection and navigation |

Do not create separate repositories for the Relay plugin, shared core, Relay UI,
or Persona App. Those boundaries would duplicate lifecycle and distribution
before they have independent owners.

## Package, Plugin, And Workspace Boundary

Relay exposes one capability through three deliberately different surfaces:

| Surface | Authority | Must not own |
| --- | --- | --- |
| Relay Core | Mail identities, evidence, memory rules, draft fingerprints, send receipts | Host navigation or plugin installation |
| Codex Plugin | Codex discovery and the `opl-relay` Skill | Package lifecycle or user data |
| OPL Package | Stable capability IDs and role-neutral `app_contributions` | Private state, credentials, approvals, or runtime truth |

The source Package descriptor is the carrier-root
`plugins/opl-relay/opl-package.json`. The installable Codex Plugin therefore
carries the owner descriptor without a second catalog. Its content lock covers
the plugin manifest and Skill, while deliberately excluding the descriptor
itself to avoid a recursive digest. App contributions contain declarative views
and opaque action references only; they never embed executable UI code.

All three surfaces resolve the same user-owned Profile Workspace:

- `OPL_PROFILE_WORKSPACE` is the single profile root;
- Relay's durable state is `<profile>/data/relay`;
- Persona's durable state is the sibling `<profile>/data/persona`;
- a source checkout, installed Package, or Codex plugin cache is never either
  authority.

### Triage evidence boundary

`opl-relay triage evidence <email-store://...> --policy-ref <ref>` reads one
already-synced message through the local email store and returns the portable
`opl-relay-mail-triage-evidence.v2` envelope. It carries the stable source
reference, `From`/`To`/`Cc`/`Bcc` header facts, recipient routing facts,
original-message readback, local-read freshness, and explicit policy references.
Its `policy_digest` is only a deterministic digest of the ordered reference set,
never a private Markdown content digest.

The envelope is evidence-only: it does not prioritize a message, infer a
personal decision, or expose a mailbox-provider write route. Its risk fields
always require human review, forbid external writes, and report provider write
as unreachable. `opl-relay triage validate --input <file|->` validates that
provenance, policy-reference digest, and read-only boundary before Persona or
another consumer uses the envelope. Persona reads private Markdown only from its
own Profile Workspace and computes its own content digest. Relay accepts either
the bare evidence envelope or
the exact successful JSON wrapper emitted by `triage evidence`, so the
read-only pipeline can be composed directly:

```bash
opl-relay --json triage evidence 'email-store://…' --policy-ref 'policy://…' \
  | opl-relay --json triage validate --input -
```

## OPL App Integration

Relay and Persona are not OPL standard agents. OPL App should consume them
through a role-neutral `app_contributions` contract:

- navigation items and views;
- commands and command palette entries;
- read models and status summaries;
- approval surfaces;
- capability and permission declarations;
- optional background services.

The Package declares contributions; OPL App renders them. The platform must not
branch on a Relay package id or force `standard_agent` fields onto a capability.

The first useful Relay contribution set is:

- inbox/triage view;
- draft review queue;
- people and relationship-memory review;
- knowledge-source status;
- sync freshness and account health;
- explicit send approval.

Apple Mail remains a supported review frontend in the Apple ecosystem. OPL App
may provide a richer review UI, but both surfaces must use the same draft
identity, fingerprint, and receipt contract.

## Persona Relationship

Relay is independently useful and independently installable. Persona may call
Relay but does not own its mail store or safety model. Persona's current job is
cross-domain judgment: maintaining the user's working context, preserving
provenance, and coordinating longer-running personal work through proposals.

This avoids two bad couplings:

- Mail does not wait for Persona to become a complete product.
- Persona does not become a monolith containing mail, knowledge, research, and
  every future domain engine.

## Delivery Sequence

1. Establish Relay branding, state/workspace separation, CLI compatibility, and
   installable Codex Plugin.
2. Add a role-neutral OPL `app_contributions` contract and a small Relay Package
   adapter in the existing OPL repos.
3. Add OPL App mail, memory, and approval views backed by Relay's existing
   identities and read models.
4. Use `opl-persona` for publication-to-knowledge/website proposals and
   Obsidian-memo-to-website/mail proposals.
5. Add adapters only at the authority that owns the target system; Persona
   remains proposal-first and does not become a central CMS or mail store.

The current repository implements the Relay-owned half of this model: the Codex
Plugin, stable capability exports, declarative Package contributions, and the
review-gated bridge that accepts a strictly validated, user-approved Persona
mail-draft context to create an Apple Mail draft without sending. Persona owns
the cross-domain proposal contract; Relay continues to own the mail draft
identity, review surface, fingerprint, and final delivery boundary.
Persona owns the cross-domain proposal contract; `gflab_web` owns its
proposal-only Hugo adapter. Framework admission, OPL App rendering, and Shell
navigation remain host-owned work in their respective repositories.
