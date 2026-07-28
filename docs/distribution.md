# OPL Relay Distribution

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
| OPL managed release | Framework repository index plus immutable payload | Not published |
| User data | Selected Profile Workspace | Local and user-owned |

The public Git repository is therefore an installation and update source for
the **Codex Git Marketplace snapshot** today. It is not yet an OPL managed
Package channel.

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

The target OPL managed lifecycle uses Framework-owned release contracts:

1. A release process publishes a complete, immutable Relay payload.
2. The Framework repository index exposes compatible candidate versions.
3. The selected index entry references a payload manifest by immutable URL and
   digest.
4. OPL Framework verifies and materializes the payload.
5. OPL App invokes Framework actions for install, update, repair, or uninstall.
6. The host passes the selected `OPL_PROFILE_WORKSPACE` when it starts Relay.
7. Installed-state readback records the exact resolved Package and carrier
   bytes.

Until a Relay release is present in that index and has passed the publication
gates, OPL App must not claim that Relay is remotely installable, current, or
repairable as a managed Package.

Relay must not implement a second updater. Version selection, immutable payload
verification, installed locks, materialization, repair, and removal belong to
OPL Framework.

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

## Release Readiness Checklist

Repository readiness:

- bilingual user-facing README;
- stable logo and license;
- full Python tests;
- Package descriptor and content-lock tests;
- Plugin manifest and Marketplace validation;
- isolated Plugin discovery and install test in CI;
- no private Profile Workspace data in Git.

Managed OPL release readiness, still outstanding:

- define the complete Relay payload contents;
- publish an immutable payload manifest and digest;
- add Relay to the Framework repository index;
- verify Framework install, update, repair, and uninstall actions;
- verify installed lock and exact-byte readback;
- verify OPL App status and action projection;
- verify that every lifecycle action preserves the selected Profile Workspace.

## Maintainer Rules

- Do not treat a passing source CI run as a published OPL Package.
- Do not point OPL App directly at a mutable branch as its Package authority.
- Do not store user data in a checkout, Plugin cache, or Package directory.
- Do not add a Relay-owned self-update implementation.
- Publish ordinary Git source changes and OPL immutable payloads as distinct
  release surfaces, each with its own readback.
