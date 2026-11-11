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

The interface uses a compact top chrome, a left workbench
navigation, a compact message list and a dedicated reading surface. Wide
screens keep source and reply side by side; intermediate screens switch between
them; narrow host panels use horizontal navigation and one content pane.
Switching between a source and its draft preserves unsaved input. The editing
state and pinned save actions remain visible, with distinct keyboard focus and
readable body, heading and metadata sizes. Layout follows viewport width; it
does not shrink typography to keep every column on screen.

The host manages this as one MiniApp surface; mail folders, connections and
drafts are internal views. Navigation and toolbars expose only implemented mail
operations. Folder scope is a label, not an inactive filter. Refresh status comes
from the owner result rather than a permanent success label. Narrow panels keep
visible navigation text, access to connected mailboxes and 44px touch targets.
Closing the host UI releases its display session; saved drafts and mailbox
connections remain owned by Relay.

The shared interaction model is defined by the Framework's
[MCP Apps specification](https://github.com/gaofeng21cn/one-person-lab/blob/main/docs/mcp-apps/SPEC.md).
Relay retains its dedicated mail editor while using the official MCP Apps SDK
for host theme, style variables, viewport constraints and display mode. Browser
mode follows the system theme. Embedded mode uses compact chrome and responsive
panes, without maintaining a second host or credentials store. Host context
changes preserve the current message, draft and unsaved input.

Loading, failures and owner results use one visible operation status. Local edit
save, mailbox draft save and delivery remain distinct outcomes. A failure keeps
the user's input; an uncertain publication or send is read back through Relay
before another attempt. Selected mail and saved draft identity/revision update
model context without sending a message or starting an Agent task. Context
notification failure does not invalidate a completed mail operation.

Opening a focused workbench resolves one account-bound mail, local draft or
server request through Relay before presenting it. A local draft focus carries
its owner revision; an explicitly stale revision is rejected with a readback
action. Reopening without a revision resolves the current saved draft. The
MCP and browser adapters preserve structured error codes, dispatch disposition
and exact recovery input. A lost mutation response is reported as uncertain
and never retried by either adapter.

Local editing records use the existing drafts ledger and optimistic revision
checks. Browser memory holds unsaved input; changing selection warns before
discarding it through an in-view dialog that also works in opaque MCP sandboxes.
Attachments are private Profile files identified by bytes and
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

### 已连接邮箱

`relay_connections` 和 `relay_connection_*` 使用同一个 Relay 领域服务。服务启动从自身已安装目录调用 Framework 公共 `activateInstalledPackageConnectionContext`，固定 consumer 为 `opl-relay`；工具及网页输入没有 Package 身份字段。“已连接邮箱”视图复用连接列表、读取授权请求、搜索和原文读取，批准仍在 OPL 的资源与连接中进行。

对应能力为 `mailbox.search`、`mailbox.read`、`draft.create`。已批准 grant 可直接调用；每次调用重新检查授权、连接版本和安装描述符。`connection_reply` 只建立原 owner 的本地审阅草稿，返回原 draft identity，不授予发送。未知结果保留 invocation ID，调用方先回读原 request。

Framework 依赖由既有 `opl packages link-framework --agent-root <installed-opl-relay> --json` 建立；构建仅将该公共导出保留为外部依赖，不复制 Framework。缺少基座或不匹配的安装根返回修复提示。Relay 原有本地邮箱入口继续遵循自己的权限与草稿合同。
