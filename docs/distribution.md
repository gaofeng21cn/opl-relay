# OPL Relay Distribution

Owner: `opl-relay`
Purpose: `distribution_boundary`
State: `active_current`
Machine boundary: Human-readable distribution and ownership map. Source
manifests, public immutable GHCR digest readback, configured-carrier state, and
fresh Codex/Relay runtime readback remain authoritative for their exact layers.

This document separates the source repository, Codex Plugin distribution, OPL
Package distribution, and user-owned data. They are related, but none is a
substitute for another.

## Current State

| Surface | Current authority | Status |
| --- | --- | --- |
| Source code | `https://github.com/gaofeng21cn/opl-relay` | Public Git repository |
| Codex Marketplace | Repository `.agents/plugins/marketplace.json` | Usable from a local clone or as a Git-backed Marketplace |
| Codex Plugin | `plugins/opl-relay` | Installable carrier containing Skill, metadata, and runtime |
| Python engine | `plugins/opl-relay/runtime` | Carried by the Plugin; no source checkout required |
| OPL Package descriptor | `plugins/opl-relay/opl-package.json` | Declares capability identity and App contributions |
| OPL Package publication | Relay owner descriptor plus immutable GHCR payload | Publication comes from public immutable digest readback; it is separate from installed state |
| User data | Selected Profile Workspace | Local and user-owned |

The public Git repository is the source and Codex Git Marketplace authority.
OPL App installation and updates use the separate GHCR Package channel.

## Codex Marketplace

Codex accepts a GitHub repository, Git URL, SSH URL, or local root as a
Marketplace source. This repository keeps its Marketplace file at:

```text
.agents/plugins/marketplace.json
```

Its current entry points to the Plugin inside the same repository:

```text
plugins/opl-relay/
```

A user can add the Git Marketplace and install the Plugin:

```bash
codex plugin marketplace add gaofeng21cn/opl-relay --ref main --json
codex plugin list --marketplace opl-relay --available --json
codex plugin add opl-relay@opl-relay --json
```

`codex plugin marketplace upgrade opl-relay` refreshes the configured Git
snapshot. Reinstalling the Plugin selects the embedded engine and Skill bytes
from that refreshed snapshot. Neither operation touches the Profile Workspace.

## Package Installation Is Separate

The file `plugins/opl-relay/opl-package.json` is a Package owner descriptor. It
declares:

- Package identity and version;
- stable Relay capabilities;
- the Codex Plugin carrier;
- role-neutral OPL App navigation, views, and commands;
- a content lock over the carrier bytes.

Its `source_repo` field records provenance. It is not a mutable updater URL and
does not tell OPL App to clone `main`.

The Package and carrier path is:

1. The package workflow publishes a complete, immutable Relay payload to GHCR.
2. OPL Base may download, verify, and hand off selected immutable OCI bytes.
3. The configured native carrier installs, updates, repairs, or removes those
   bytes and owns physical installed state.
4. OPL Framework discovers installed descriptors, delegates carrier actions,
   and aggregates presence and callability without creating a second resolver,
   installed lock, payload store, LKG, or lifecycle receipt.
5. OPL App renders Framework's generic projection and invokes only the actions
   supplied by the configured carrier.
6. The host passes the selected `OPL_PROFILE_WORKSPACE` when it starts Relay.
7. Native-carrier readback records the exact resolved Package and carrier
   bytes.

The contracted moving channel is:

```text
ghcr.io/gaofeng21cn/one-person-lab-packages/opl-relay:latest-stable
```

Publication must be read from the public immutable GHCR digest. Installed,
current, repairable, or removable state must be read separately from the
configured native carrier. Framework projection aggregates these owner results;
it is not an installation or update authority. GitHub Releases are not an
installation or update authority.

Relay must not implement a second updater. Relay owns descriptor identity and
publication; OPL Base keeps only thin download/verify/handoff behavior; the
configured native carrier owns physical lifecycle and readback; Framework owns
generic discovery, delegation, and aggregation.

## Profile Workspace

Package installation and user data have different lifecycles.

```text
Package installation
  replaceable code, Skills, manifests, and dependencies

Profile Workspace
  user identity, policies, context, mail, memory, proposals, and receipts
```

Relay receives one selector:

```bash
OPL_PROFILE_WORKSPACE=/absolute/path/to/profile
```

It stores module data only under:

```text
<profile>/data/relay/
```

Persona uses `<profile>/data/persona/`. Both modules share the same person's
Profile Workspace without sharing or overwriting their private state files.

The following never belong in the source repository, immutable Package payload,
Plugin snapshot, or installed-package lock:

- account configuration and credential references;
- raw mail and SQLite databases;
- draft ledgers and send receipts;
- approved relationship memory;
- synchronization cursors;
- private policies and Obsidian paths;
- credentials or Keychain exports.

Install, update, repair, and uninstall must preserve the Profile Workspace.

## Distribution Verification

Repository readiness:

- bilingual user-facing README;
- stable logo and license;
- full Python tests;
- Package descriptor and content-lock tests;
- Plugin manifest and Marketplace validation;
- isolated Plugin discovery and install test in manual CI qualification;
- no private Profile Workspace data in Git.

Managed Package verification requires all of the following:

- the immutable version and `latest-stable` resolve to the same public digest;
- the configured native carrier exposes the intended lifecycle actions;
- native installed exact-byte readback matches the selected payload;
- Framework aggregation matches the carrier result without a second lock,
  resolver, or currentness source;
- OPL App renders Relay through the generic Capability Package projection;
- every lifecycle action preserves the selected Profile Workspace.

## Maintainer Rules

- Do not treat a passing source CI run as a published OPL Package.
- Do not point OPL App directly at a mutable branch as its Package authority.
- Do not store user data in a checkout, Plugin cache, or Package directory.
- Do not add a Relay-owned self-update implementation.
- Publish ordinary Git source changes and OPL immutable payloads as distinct
  surfaces, each with its own readback. Do not create a GitHub Release.
