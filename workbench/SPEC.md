# Relay Mail Workbench

The workbench gives the user and Codex a shared surface to read an exact mail
thread, prepare Reply All, edit a draft and its attachments, review the complete
message, and explicitly save or send it. The existing Relay mailbox store,
Profile Workspace, draft ledger, Himalaya transport and delivery checks remain
the owners. No MiniMax business code or artwork is copied.

The same UI is served as a standard MCP App and a loopback browser page. The
Codex Plugin bundles its MCP server. Hosts without MCP App rendering open the
same page in their built-in browser. Host rendering is verified separately from
MCP protocol and browser acceptance. UI capability negotiation selects the
inline resource or an explicit browser presentation; ordinary MCP clients keep
mail-read and local-draft tools while App-only tools are withheld. Generic,
Codex and OPL configurations, plus DSH's configuration-only native bundle,
point to one installed engine and one selected Profile. OPL App's existing
collection commands hand the same browser view to Studio; no host-specific mail
UI or second draft authority is introduced.

The first view contains account selection, Inbox/Sent/history search and Relay
drafts. Selecting a message shows full headers, readable quoted context and
attachments. Reply All derives recipients from the exact source and preserves
the source reference. New reply text is edited separately from the approved
signature and immutable quoted history. The editor preserves plain-text line
breaks; incoming HTML is sanitized with DOMPurify in a sandboxed document.

The neutral white interface uses a compact top chrome, a left workbench
navigation, a card-based message list and a dedicated reading surface. Wide
screens keep source and reply side by side; intermediate screens switch between
them; narrow host panels use horizontal navigation and one content pane.
Switching between a source and its draft preserves unsaved input. The editing
state and pinned save actions remain visible, with distinct keyboard focus and
readable body, heading and metadata sizes. Layout follows viewport width; it
does not shrink typography to keep every column on screen.

Local editing records use the existing drafts ledger and optimistic revision
checks. Browser memory holds unsaved input; changing selection warns before
discarding it. Attachments are private Profile files identified by bytes and
hash. Saving to the mailbox creates a stable server draft request. Existing
server drafts are reviewable; their immutable content is never overwritten.
To change published content, start a new reply from its exact source.

Sending requires a current live server draft, a complete review including all
recipients and attachments, the matching fingerprint, and an explicit click.
The current Apple Mail delivery adapter remains in use. An unknown outcome is
inspected and never resent. No real recipients are used for functional tests.

The local web server binds loopback, authenticates every data request with a
per-process capability, checks request origin and content type, and serves only
bundled resources. The MCP adapter uses the official SDK and the standard App
bridge. Neither adapter accepts shell commands, arbitrary paths, mailbox
deletion, or credential values. Both invoke the same bounded Relay CLI actions.
