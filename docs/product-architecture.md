# OPL Relay, Persona, And App Product Architecture

## Decision

Use one long-lived mail repo now: `opl-relay`. Create `opl-persona` only after a
first non-mail domain proves that cross-domain orchestration is a real product
boundary. Keep OPL App as the long-term user entry rather than building a second
Persona-specific macOS shell.

## Product Model

```text
OPL App
  visual mail, memory, knowledge, approval, and work views
  generic Package contribution host
        |
        v
Codex / OPL app-server runtime
  chat-first reasoning, tools, approvals, session continuity
        |
        +--> OPL Persona Skills (future cross-domain orchestration)
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
| `opl-relay` | Rename current repo | Mail engine, CLI, Relay Skill/plugin, future Package adapter |
| `opl-persona` | Defer | Cross-domain Persona Skills and orchestration after a second domain exists |
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

The source Package descriptor is
`packages/opl-relay/package.json`. Its content lock covers the installable
plugin manifest and Skill, while its App contributions contain declarative
views and opaque action references only. It never embeds executable UI code.

All three surfaces resolve the same user-owned runtime:

- `OPL_RELAY_HOME` is the long-lived private data authority;
- `OPL_RELAY_WORKSPACE` is the replaceable human context;
- a source checkout, installed Package, or Codex plugin cache is never either
  authority.

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
Relay but does not own its mail store or safety model. Persona's future job is
cross-domain judgment: deciding which capability to invoke, maintaining the
user's working context, and coordinating longer-running personal work.

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
4. Create Persona only when a second non-mail domain needs shared orchestration.
5. Decide whether Persona itself needs a Package after actual cross-domain
   behavior exists.

The current repository implements step 1 and the Relay-owned half of step 2:
the Codex Plugin, stable capability exports, and declarative Package
contributions now live together without owning user data. Framework admission,
OPL App rendering, and Shell navigation remain host-owned work in their
respective repositories; steps 3-5 are not smuggled into the Relay engine.
