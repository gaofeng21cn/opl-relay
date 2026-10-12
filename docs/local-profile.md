# Local Data And Workspace

Owner: `opl-relay`
Purpose: `profile_workspace_boundary`
State: `active_current`
Machine boundary: Human-readable profile and data-location guide. The effective
selector and runtime paths remain in current Relay source and fresh
`opl-relay --json doctor` readback; private user bytes never derive from Git.

OPL Relay separates installed capability, durable user data, and task context.
Do not place new private state under a Git checkout or plugin directory.

## Data Root

This guide owns workspace setup and explicit overlay copying. The default
root, selector and durable-state invariants belong to
[Workspace Contract](workspace-contract.md). Relay stores module state under
the selected workspace's `data/relay` directory.

Credentials remain in macOS Keychain service `codex-mail-workbench`. The service
name is a local credential contract; it is never exported to Git or the Profile
Workspace. An IMAP account may opt in to an explicitly provisioned unattended
copy in `/Library/Keychains/System.keychain` by setting
`fallback_keychain = "/Library/Keychains/System.keychain"` in its `[accounts.imap]`
section. Relay reads that copy directly, without first probing the interactive
login keychain, and sync reports `credential_source = "system_fallback"`.
If the configured copy fails, the operation stops rather than opening a login
keychain prompt. IMAP accounts without this setting use the login keychain.
Provision the same service,
account reference and secret in the System keychain, restrict its trusted reader
to `/usr/bin/security`, and verify a normal-user read before enabling fallback.
On password rotation, update and read back both copies before resuming sync.
This setting does not bypass IMAP authentication or make a locked System
keychain readable; a failed credential read stops the operation.

## Hotmail And Outlook.com

Microsoft accounts use delegated Microsoft Graph OAuth, not an IMAP password.
Register a personal-account public/native application in your Microsoft Entra
directory with the redirect URI `http://localhost`. No client secret is needed.
Configure the application ID as metadata, then complete the browser consent:

```bash
opl-relay --json account add --id personal --email you@hotmail.com \
  --provider microsoft_graph --client-id <application-client-id> \
  --include '*' --exclude 'Junk Email' --exclude 'Deleted Items' \
  --exclude Drafts --exclude Outbox
opl-relay --json account login --account personal
opl-relay --json account check --account personal --connect
opl-relay --json account folders --account personal
opl-relay --json sync --account personal --scope active
```

`account login` prints the authorization URL and waits up to ten minutes for its
loopback callback. Open that URL in your browser. It requests `User.Read`,
`Mail.Read` and `offline_access`; the configured email must match `/me` before
tokens are saved. Access/refresh tokens stay in login Keychain and never enter
accounts TOML, SQLite, source, or command arguments. Long credentials use the
macOS Security API; background renewal retains the trusted Relay interpreter
and `/usr/bin/security` reader without weakening global Keychain protections.

Use `account login --account personal --write` when authorizing mailbox
organization. This requests `Mail.ReadWrite`, not `Mail.Send`. Permission alone
does not authorize moving messages: exact references, live preflight, explicit
`mailbox move --apply`, an operation receipt, and source/target verification
remain required. Existing Archive/Bill descendants can be selected through
`--destination-folder`; Relay does not create folders or change importance,
flags, or read state. The existing server-draft commands also support Graph:
the user can review and send in their synchronized mail client. Creating a
draft never sends, and phone edits are preserved.
After an unknown Graph move, use the original `mailbox move` arguments with
`--reconcile` instead of `--apply`. This reads the actual immutable identity,
position and MIME and saves a confirmed receipt without another remote write.

Graph sync uses immutable message IDs, MIME evidence and folder delta cursors
in the same Relay store. Interrupted pages retry their missing bodies; an
expired delta cursor rebuilds membership without discarding old evidence.
Daily `review prepare` reads active folders only. Prefer `search --remote
--account personal --scope history <query>` for requested history; it downloads
only matching MIME into the same evidence store. Explicit `sync --scope history`
remains available when offline historical coverage is actually required.
`--max-seconds` bounds each invocation, not a daily message quota. `--dry-run`
enumerates folders without fetching messages or writing provider cursors.

Official protocol contracts: [delegated OAuth](https://learn.microsoft.com/en-us/graph/auth-v2-user),
[immutable Outlook IDs](https://learn.microsoft.com/en-us/graph/outlook-immutable-id),
[folder delta](https://learn.microsoft.com/en-us/graph/api/message-delta),
and [message movement](https://learn.microsoft.com/en-us/graph/api/message-move).

## Mature Mail Backend

Himalaya is the sole mail transport backend. Accounts default to
`backend = "himalaya"`; the retired `builtin` selector is rejected. Configure a pinned Himalaya 2.2.x
executable in `<profile>/data/relay/tools.toml`:

```toml
[himalaya]
executable = "/absolute/path/to/himalaya"
```

Relay generates a disposable account configuration from its canonical metadata.
Credentials remain in Keychain and are resolved into the child environment;
they never appear in config files, argv, logs or receipts. IMAP uses LOGIN over
TLS; Graph uses the existing identity-bound, renewable bearer credential.
OAuth reads and writes use the authorized macOS Security API without placing
tokens in command output. Renewal preserves an existing Keychain access list.
Background OAuth reads and renewal disable Keychain interaction and return an
authorization error instead of opening password dialogs. Only explicit
`account login` permits initial Keychain authorization. Refresh-lock waits
respect the operation's time budget.
For unattended use, the stable launcher must select the same supported Python
interpreter used for initial Keychain authorization. Do not rely on a terminal's
`PATH`: launchd may otherwise choose an older system interpreter. Validate the
actual background entry point, as well as the interactive CLI.
The versioned [stable host launcher](../scripts/opl-relay-launcher.sh) resolves
an already-installed Codex Plugin carrier and explicitly invokes
`$HOME/.local/bin/python3`. Set `OPL_RELAY_PYTHON` to an absolute executable path
when the authorized interpreter lives elsewhere; Python 3.11 or newer is required.
`OPL_RELAY_CARRIER_ROOT` can select another installed carrier directory; otherwise
the launcher uses `${CODEX_HOME:-$HOME/.codex}/plugins/cache/opl-relay/opl-relay`.
Provision this host entry point once at the stable path used by the mail jobs.
It is not a package installer or updater, and contains no Profile or credentials.
Configured native operations fail explicitly rather than silently falling back.
`account check --connect` validates the native backend as well as the provider.

Himalaya handles authentication/transport, folder listings, IMAP UID snapshots,
MIME reads, review-draft creation and movement. Relay verifies its structured
receipts and retains Graph OAuth renewal, delta and ImmutableId binding because
the CLI does not expose those management contracts. No Python IMAP connection,
direct Graph mail writer or automatic legacy fallback remains. On UIDPLUS-only
servers, Relay verifies the copied target before asking Himalaya to remove the
exact source UID; it never requests an unscoped EXPUNGE. Native search uses `search --remote`
with an explicit account; it caches only selected matches, does not advance a
folder snapshot or review progress, and reports errors or result truncation.
`sync` defaults to active scope. Archive is never a daily review queue.
IMAP directory selection accepts existing modified UTF-7 configuration names;
SPECIAL-USE roles identify localized Sent and Archive directories. A normal sync
rebinds cached locations while preserving previously issued mail references and
review records. `account folders` supports both IMAP and Graph and reports the
actual remote directory tree without downloading messages.

Gmail can presently use Himalaya through existing IMAP credentials. Native
Gmail API via gog requires its own Google OAuth client and consent; an IMAP app
password is not a Google API token. Do not claim a gog cutover before that
account is authorized and server readback passes. Tool replacement must preserve
current `email-store://` evidence and review records rather than creating a
second assistant-specific store.

## Profile Workspace

`OPL_PROFILE_WORKSPACE` selects the active Profile Workspace.

```text
<workspace>/
  .opl-profile-workspace.json
  AGENTS.md
  profile/
  policies/
  context/
  templates/
  exports/
  data/relay/
  data/persona/
```

Initialize or inspect it:

```bash
opl-relay --json workspace init
opl-relay --json workspace inspect
```

Select a Profile Workspace without moving the mail database:

```bash
export OPL_PROFILE_WORKSPACE=/path/to/profile
opl-relay --json doctor
```

## Copying An Explicit Overlay

Preview first, then copy the supported human-editable overlay:

```bash
OPL_PROFILE_WORKSPACE=/path/to/profile \
  opl-relay --json workspace migrate --from /path/to/source-overlay
OPL_PROFILE_WORKSPACE=/path/to/profile \
  opl-relay --json workspace migrate --from /path/to/source-overlay --apply
```

The migration copies `AGENTS.md`, `profile.md`, and the `profile`, `policies`,
`context`, and `templates` trees. It reports and skips accounts,
SQLite databases, `sources.toml`, and sync state. It refuses conflicting target
bytes and verifies every copied file after writing. It does not delete the
source; source cleanup remains an explicit, separately verified operation.

## Obsidian

Configure Obsidian roots in the data root's `sources.toml`. The provider is
read-only and writes only a rebuildable derived index into `memory.sqlite`.
Use narrow include paths:

```bash
opl-relay --json sources list
opl-relay --json sources index
opl-relay --json sources search "<person or project>"
```

## Publication Check

Before committing, verify that accounts, database files, raw messages, private
workspace content, Obsidian paths, credentials, and sync cursors are absent from
Git.
