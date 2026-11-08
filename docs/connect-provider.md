# Relay Connect Provider

`opl-relay --json connect` is the Relay-owned mailbox provider bridge for Framework and shared Agent/Mini App consumers. It accepts one JSON request on stdin and writes one JSON envelope to stdout. It does not expose send or server-draft publication.

## Request

Every request carries `schema_version: "opl-relay-connect.v1"`.

```json
{"schema_version":"opl-relay-connect.v1","operation":"list"}
{"schema_version":"opl-relay-connect.v1","operation":"inspect","mailbox_ref":"relay-mailbox://..."}
{"schema_version":"opl-relay-connect.v1","operation":"invoke","credential_handle":{"opaque":"relay-mailbox://...","revision":"..."},"capability":"mailbox.search","input":{"query":"meeting","scope":"active","limit":20}}
```

`list` returns configured mailbox references, account identifiers, display addresses, and account revisions. It returns no credential material. `inspect` resolves a reference against the current Relay profile and returns the same mailbox binding with its available capabilities. `invoke` requires the opaque reference and current revision returned by Relay; a caller cannot choose an account through `input`.

## Capabilities

- `mailbox.search` searches Relay's synchronized local cache. Input accepts `query`, `scope` (`active` or `history`), `folder`, and `limit` (1..100). The result identifies `source: "relay-local-cache"` and carries the existing snapshot freshness.
- `mailbox.read` reads an `email-store://` reference from Relay's local cache and verifies that it belongs to the bound mailbox. The result identifies `source: "relay-local-cache"`.
- `draft.create` creates or returns an idempotent local Reply All review draft from a bound source reference. Input accepts `storage_ref`, `body`, and `request_id`. The result identifies `persistence: "relay-local-review"` and always returns `send_allowed: false`.

Connection setup, Keychain credentials, Himalaya transport, synchronization, and server draft operations remain owned by Relay. The bridge neither reads secrets directly nor accepts caller-supplied credentials. Mailbox reads here do not initiate remote synchronization.

## Response And Failure

Successful calls return `{ "ok": true, "disposition": "completed", "result": ... }`. Failures return `{ "ok": false, "disposition": ..., "error": { "code": ..., "message": ... } }`. `not_dispatched` means no operation reached the local operation owner. `unknown_after_dispatch` means a local draft write may have started; inspect the same `request_id` before retrying. Errors use stable messages and never echo exception text, request arguments, credentials, or subprocess stderr.

Supported error codes are `INVALID_REQUEST`, `INVALID_CREDENTIAL_HANDLE`, `STALE_CREDENTIAL_HANDLE`, `MAILBOX_NOT_FOUND`, `INVALID_ARGUMENTS`, `CAPABILITY_UNAVAILABLE`, and `PROVIDER_ERROR`.

The bridge uses the installed profile selected by Relay's existing `OPL_PROFILE_WORKSPACE` contract. Tests use temporary profiles and seeded `.example.test` messages. Never use a real mailbox to qualify this interface.
