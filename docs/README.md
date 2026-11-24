# OPL Relay Documentation

Family-wide documentation lifecycle rules live in the [OPL docs lifecycle policy](https://github.com/gaofeng21cn/one-person-lab/blob/main/docs/policies/docs-lifecycle-policy.md). Each document in `opl-relay` owns one topic:

| Document | Sole responsibility |
| --- | --- |
| [English README](../README.md) / [中文 README](../README.zh-CN.md) | Product overview, installation, quickstart commands, safety summary, and verification entry |
| [Repository agent rules](../AGENTS.md) | Maintainer constraints, storage/safety boundaries, and documentation rules |
| [Architecture](architecture.md) | Internal engine layers, runtime ownership & workspace invariants, workbench, server drafts, mail/memory currentness, and App ABI |
| [Local Profile & Operations](local-profile.md) | Keychain/Graph OAuth setup, Himalaya & host launcher config, Profile workspace setup/migration, and Obsidian/Markdown knowledge CLI |
| [Distribution](distribution.md) | Codex Marketplace, Workbench MCP App build & host adapters, OPL Package publication, and GHCR OCI boundary |
| [Product Architecture](product-architecture.md) | Cross-repository handoffs across Relay, Persona, Framework, App, and Studio |
| [Connect Provider](connect-provider.md) | `opl-relay --json connect` (`opl-relay-connect.v1`) stdin/stdout bridge specification |
| [Workbench Specification](../workbench/SPEC.md) | Shared Mail Workbench MCP App & loopback UI interaction contract |
