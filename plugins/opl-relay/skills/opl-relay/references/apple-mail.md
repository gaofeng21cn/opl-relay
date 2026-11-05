# Apple Mail Drafts

Read this only for Mail.app-specific composition, manual attachments, desktop-local threads, or UI diagnosis. Routine server draft composition and approved delivery use the main Skill.

`draft create` and `draft reply-all` may create and open an Apple Mail draft.
They do not authorize sending. Let the user review the draft in Apple Mail, run
`draft inspect` again after approval, and pass only the current fingerprint to
`draft send`. Any content change invalidates approval. Never retry an `unknown`
send result.

Use the Apple Mail route when the user specifically asks for Mail.app, when a
draft already contains manual edits or attachments that must be preserved, or
when the desktop client owns a local-only thread. Do not use rich-text text
selection to rewrite an existing Apple Mail draft; selection and replacement
in the web view can fail partially, leaving duplicated or joined paragraphs.
Change the source prose and rebuild the draft through the native Reply All route
or the server Drafts route instead.

For a reply in an existing multi-recipient Apple Mail thread, Reply All is the
default workflow. Use Mail's native Reply All route rather than creating a new
message or rebuilding recipients from a flattened Relay record. A private reply
is appropriate only when the user explicitly requests it or confidentiality
clearly requires it.

When visible Apple Mail interaction is needed, use Computer Use: open
the exact source message, invoke the visible native Reply All control, and paste
the reviewed response at Mail's default insertion point. In Mail's rich-text
editor, do not place the cursor by clicking near the first character of the
signature or use coordinate-based insertion at the signature boundary. This
can leave a stray character in the greeting, remove the first character of the
signature, or introduce unwanted line breaks. If the default insertion point is
unavailable, recreate the Reply All draft from the original message instead of
repairing the body around the signature. Relay remains the
authority for source lookup, account and recipient readback, draft adoption,
thread validation, and approval gating. Do not use background body replacement
or construct a same-subject outgoing message as a substitute for Reply All.

Treat the thread context as part of the reply, not as optional decoration:

1. Preserve Mail's complete native To/Cc route. Do not manually remove existing
   participants merely because they are not the primary follow-up owner.
2. Preserve the quoted message chain and the provider threading headers. Do not
   replace the composer body with body-only text.
3. Insert only the new response above the account signature. Do not include a
   hand-written copy of the configured Apple Mail signature in `--body-file`.
4. After saving, reread the Apple Mail draft and verify To/Cc, subject, the
   greeting's first character, expected paragraph breaks, exactly one intact
   signature, `In-Reply-To`/`References`, and at least one recognizable
   quoted-thread anchor before reporting the draft ready. Confirm that
   user-added attachments remain present and unchanged.
5. If any recipient or quoted context is missing, stop and rebuild from the
   original message with native Reply All. Do not repair the route from memory.

Functional testing must use a dedicated low-consequence thread whose recipients
are test addresses or people explicitly approved for the test. Never use an
active editorial, conference, clinical, administrative, or collaboration thread
with real multi-recipient routing merely because the test does not intend to
send. A diagnostic marker in the body is not a safety control. Before the UI
test, read back To/Cc and confirm the test boundary; never invoke Send. After the
test, discard the draft through the provider path and verify all three surfaces:
no matching Drafts item, no compose window, and no diagnostic marker in Sent.
Important live threads may be used only for read-only comparison after the
workflow has passed on the controlled test thread, or for the actual requested
draft content.

```bash
opl-relay --json draft reply-all \
  --account work \
  --apple-mail-account 'Work' \
  --apple-mail-id 12345 \
  --mailbox-path 'INBOX/Conference' \
  --body-file ./reply.txt
```

The Apple Mail `account`, numeric message `id`, and `mailboxPath` must be the
exact tuple returned by the local screen. Relay invokes Mail's visible native
Reply All editor so Mail owns the signature and quoted chain. It fails closed
if the route contains the sender's own address, duplicates, Bcc, no recipients,
an ambiguous source, or if saved readback lacks `In-Reply-To`, `References`, the
source quote anchor, or exactly one current signature above the quote. Sending
still requires the separate fingerprint-bound `draft send` operation.
