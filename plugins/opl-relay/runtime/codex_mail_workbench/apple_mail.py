from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from email import policy
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from .drafts import Attachment, DraftSnapshot, Recipient, SendStart, normalize_body


class AppleMailError(RuntimeError):
    pass


JXA_SOURCE = r"""
function lower(value) {
  return String(value || "").toLowerCase();
}

function normalizedContent(value) {
  return String(value || "")
    .replace(/\r\n?/g, "\n")
    .replace(/[\u2028\u2029]/g, "\n")
    .replace(/^\n/, "")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/[ \t\n]+$/, "");
}

function addresses(items) {
  var out = [];
  for (var i = 0; i < items.length; i++) {
    out.push({
      address: String(items[i].address() || ""),
      name: String(items[i].name() || "")
    });
  }
  return out;
}

function attachmentRows(message) {
  var out = [];
  var items = [];
  try {
    items = message.mailAttachments();
  } catch (error) {
    try {
      items = message.content.attachments();
    } catch (ignored) {
      items = [];
    }
  }
  if (!items) {
    items = [];
  }
  for (var i = 0; i < items.length; i++) {
    try {
      out.push({
        name: String(items[i].name ? items[i].name() : ""),
        mimeType: String(items[i].mimeType ? items[i].mimeType() : ""),
        fileSize: Number(items[i].fileSize ? items[i].fileSize() : 0),
        id: String(items[i].id ? items[i].id() : "")
      });
    } catch (error) {
      continue;
    }
  }
  return out;
}

function uuidFromHeaders(headers) {
  var match = String(headers || "").match(
    /^X-Universally-Unique-Identifier:\s*(.+?)\s*$/mi
  );
  return match ? String(match[1]).trim() : "";
}

function guardForMessage(message) {
  return {
    sender: String(message.sender() || ""),
    to: addresses(message.toRecipients()),
    cc: addresses(message.ccRecipients()),
    bcc: addresses(message.bccRecipients()),
    subject: String(message.subject() || ""),
    content: normalizedContent(message.content()),
    attachments: attachmentRows(message)
  };
}

function draftRow(message, account, mailbox) {
  var headers = String(message.allHeaders() || "");
  return {
    providerAccount: String(account.name()),
    mailbox: String(mailbox.name()),
    id: Number(message.id()),
    uuid: uuidFromHeaders(headers),
    messageId: String(message.messageId() || ""),
    sender: String(message.sender() || ""),
    to: addresses(message.toRecipients()),
    cc: addresses(message.ccRecipients()),
    bcc: addresses(message.bccRecipients()),
    subject: String(message.subject() || ""),
    content: String(message.content() || ""),
    attachments: attachmentRows(message),
    source: String(message.source() || ""),
    guard: guardForMessage(message)
  };
}

function accountForSender(app, sender) {
  var matches = [];
  var accounts = app.accounts();
  for (var i = 0; i < accounts.length; i++) {
    var emails = accounts[i].emailAddresses();
    for (var j = 0; j < emails.length; j++) {
      if (lower(emails[j]) === lower(sender)) {
        matches.push(accounts[i]);
        break;
      }
    }
  }
  if (matches.length !== 1) {
    throw new Error(
      "Expected one Apple Mail account for sender " + sender +
      ", found " + matches.length
    );
  }
  return matches[0];
}

function mailboxByRole(account, role) {
  var candidates = role === "drafts"
    ? ["Drafts", "Draft", "草稿箱", "草稿"]
    : ["Sent Messages", "Sent", "Sent Mail", "已发送邮件", "已发件箱", "已发送"];
  var boxes = account.mailboxes();
  for (var i = 0; i < candidates.length; i++) {
    for (var j = 0; j < boxes.length; j++) {
      if (lower(boxes[j].name()) === lower(candidates[i])) {
        return boxes[j];
      }
    }
  }
  throw new Error("Apple Mail " + role + " mailbox not found for " + account.name());
}

function accountByName(app, name) {
  var accounts = app.accounts();
  for (var i = 0; i < accounts.length; i++) {
    if (String(accounts[i].name()) === String(name)) {
      return accounts[i];
    }
  }
  throw new Error("Apple Mail account not found: " + name);
}

function mailboxByPath(account, path) {
  var parts = String(path || "").split("/").filter(function (part) {
    return String(part).trim() !== "";
  });
  if (parts.length === 0) {
    throw new Error("Apple Mail mailboxPath is required");
  }
  var boxes = account.mailboxes();
  var current = null;
  for (var i = 0; i < parts.length; i++) {
    var matches = [];
    for (var j = 0; j < boxes.length; j++) {
      if (lower(boxes[j].name()) === lower(parts[i])) {
        matches.push(boxes[j]);
      }
    }
    if (matches.length !== 1) {
      throw new Error(
        "Expected one Apple Mail mailbox for path " + path +
        " at " + parts[i] + ", found " + matches.length
      );
    }
    current = matches[0];
    boxes = current.mailboxes();
  }
  return current;
}

function messageById(mailbox, id) {
  var wanted = Number(id);
  if (!Number.isFinite(wanted)) {
    throw new Error("Apple Mail source message id must be numeric");
  }
  var matches = [];
  var messages = mailbox.messages();
  for (var i = 0; i < messages.length; i++) {
    try {
      if (Number(messages[i].id()) === wanted) {
        matches.push(messages[i]);
      }
    } catch (error) {
      continue;
    }
  }
  if (matches.length !== 1) {
    throw new Error(
      "Expected one Apple Mail source message id " + wanted +
      ", found " + matches.length
    );
  }
  return matches[0];
}

function disableSignature(message) {
  try {
    message.messageSignature = null;
  } catch (error) {
    try {
      message.messageSignature.set(null);
    } catch (ignored) {}
  }
}

function validateReplyRecipients(account, message) {
  var own = {};
  var accountAddresses = account.emailAddresses();
  for (var i = 0; i < accountAddresses.length; i++) {
    own[lower(accountAddresses[i])] = true;
  }
  var seen = {};
  var count = 0;
  var groups = [message.toRecipients(), message.ccRecipients()];
  for (var groupIndex = 0; groupIndex < groups.length; groupIndex++) {
    for (var itemIndex = 0; itemIndex < groups[groupIndex].length; itemIndex++) {
      var address = lower(groups[groupIndex][itemIndex].address());
      if (!address) {
        throw new Error("Apple Mail Reply All produced an empty recipient address");
      }
      if (own[address]) {
        throw new Error("Apple Mail Reply All retained the sender's own address");
      }
      if (seen[address]) {
        throw new Error("Apple Mail Reply All produced a duplicate recipient: " + address);
      }
      seen[address] = true;
      count += 1;
    }
  }
  if (message.bccRecipients().length !== 0) {
    throw new Error("Apple Mail Reply All unexpectedly produced Bcc recipients");
  }
  if (count === 0) {
    throw new Error("Apple Mail Reply All produced no recipients");
  }
}

function findDraft(mailbox, uuid) {
  var messages = mailbox.messages();
  for (var i = 0; i < messages.length; i++) {
    try {
      if (uuidFromHeaders(messages[i].allHeaders()) === String(uuid)) {
        return messages[i];
      }
    } catch (error) {
      continue;
    }
  }
  return null;
}

function normalizedMessageId(value) {
  return lower(String(value || "").replace(/^</, "").replace(/>$/, ""));
}

function sameGuard(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function routingKey(message) {
  return {
    sender: String(message.sender() || ""),
    to: addresses(message.toRecipients()),
    cc: addresses(message.ccRecipients()),
    bcc: addresses(message.bccRecipients()),
    subject: String(message.subject() || "")
  };
}

function outgoingForRoute(app, expected) {
  var matches = [];
  var outgoing = app.outgoingMessages();
  for (var i = 0; i < outgoing.length; i++) {
    try {
      if (sameGuard(routingKey(outgoing[i]), expected)) {
        matches.push(outgoing[i]);
      }
    } catch (error) {
      continue;
    }
  }
  return matches.length === 1 ? matches[0] : null;
}

function outgoingForGuard(app, expected) {
  var matches = [];
  var outgoing = app.outgoingMessages();
  for (var i = 0; i < outgoing.length; i++) {
    try {
      if (sameGuard(guardForMessage(outgoing[i]), expected)) {
        matches.push(outgoing[i]);
      }
    } catch (error) {
      continue;
    }
  }
  if (matches.length > 1) {
    throw new Error("More than one outgoing message matches the approved draft");
  }
  return matches.length === 1 ? matches[0] : null;
}

function pushRecipients(app, message, kind, items) {
  for (var i = 0; i < items.length; i++) {
    var props = {address: items[i].address};
    if (items[i].name) {
      props.name = items[i].name;
    }
    if (kind === "to") {
      message.toRecipients.push(app.ToRecipient(props));
    } else if (kind === "cc") {
      message.ccRecipients.push(app.CcRecipient(props));
    } else {
      message.bccRecipients.push(app.BccRecipient(props));
    }
  }
}

function createDraft(app, payload) {
  var account = accountForSender(app, payload.sender);
  var drafts = mailboxByRole(account, "drafts");
  var baseline = {};
  var oldMessages = drafts.messages();
  for (var i = 0; i < oldMessages.length; i++) {
    try {
      baseline[uuidFromHeaders(oldMessages[i].allHeaders())] = true;
    } catch (error) {
      continue;
    }
  }

  var message = app.OutgoingMessage({
    sender: payload.sender,
    subject: payload.subject,
    content: payload.bodyText,
    visible: Boolean(payload.visible)
  });
  app.outgoingMessages.push(message);
  // Account signatures are deliberately disabled. The reviewed body is the
  // complete body; Mail must not append mutable account-local content later.
  disableSignature(message);
  pushRecipients(app, message, "to", payload.to || []);
  pushRecipients(app, message, "cc", payload.cc || []);
  pushRecipients(app, message, "bcc", payload.bcc || []);
  for (var j = 0; j < (payload.attachments || []).length; j++) {
    message.content.attachments.push(
      app.Attachment({fileName: Path(payload.attachments[j])})
    );
  }
  app.save(message);

  for (var attempt = 0; attempt < 30; attempt++) {
    delay(0.2);
    var current = drafts.messages();
    var matches = [];
    for (var k = 0; k < current.length; k++) {
      try {
        var uuid = uuidFromHeaders(current[k].allHeaders());
        if (!baseline[uuid] && String(current[k].subject()) === String(payload.subject)) {
          matches.push(current[k]);
        }
      } catch (error) {
        continue;
      }
    }
    if (matches.length === 1) {
      return draftRow(matches[0], account, drafts);
    }
    if (matches.length > 1) {
      throw new Error("More than one new Apple Mail draft matched the request");
    }
  }
  throw new Error("Apple Mail did not persist the new draft");
}

function replyAllDraft(app, payload) {
  var account = accountByName(app, payload.providerAccount);
  var senderAccount = accountForSender(app, payload.sender);
  if (String(senderAccount.name()) !== String(account.name())) {
    throw new Error(
      "Relay sender account does not match Apple Mail source account: " +
      senderAccount.name() + " != " + account.name()
    );
  }
  var sourceMailbox = mailboxByPath(account, payload.mailboxPath);
  var sourceMessage = messageById(sourceMailbox, payload.sourceMessageId);
  var drafts = mailboxByRole(account, "drafts");
  var baseline = {};
  var oldMessages = drafts.messages();
  for (var i = 0; i < oldMessages.length; i++) {
    try {
      baseline[uuidFromHeaders(oldMessages[i].allHeaders())] = true;
    } catch (error) {
      continue;
    }
  }

  var nativeReply = null;
  var reviewDraft = null;
  try {
    nativeReply = app.reply(sourceMessage, {
      openingWindow: false,
      replyToAll: true
    });
    if (!nativeReply) {
      throw new Error("Apple Mail did not create a native Reply All message");
    }
    delay(2);
    validateReplyRecipients(account, nativeReply);
    var route = routingKey(nativeReply);
    var quotedContent = normalizedContent(nativeReply.content());
    var reviewedBody = normalizedContent(payload.bodyText);
    var materializedBody = reviewedBody +
      (quotedContent ? "\n\n" + quotedContent : "");

    reviewDraft = app.OutgoingMessage({
      sender: payload.sender,
      subject: route.subject,
      content: materializedBody,
      visible: Boolean(payload.visible)
    });
    app.outgoingMessages.push(reviewDraft);
    disableSignature(reviewDraft);
    pushRecipients(app, reviewDraft, "to", route.to);
    pushRecipients(app, reviewDraft, "cc", route.cc);
    pushRecipients(app, reviewDraft, "bcc", route.bcc);
    for (var attachmentIndex = 0;
         attachmentIndex < (payload.attachments || []).length;
         attachmentIndex++) {
      reviewDraft.content.attachments.push(
        app.Attachment({fileName: Path(payload.attachments[attachmentIndex])})
      );
    }
    validateReplyRecipients(account, reviewDraft);
    app.delete(nativeReply);
    nativeReply = null;
    app.save(reviewDraft);

    var expectedRoute = routingKey(reviewDraft);
    for (var attempt = 0; attempt < 30; attempt++) {
      delay(0.2);
      var current = drafts.messages();
      var matches = [];
      for (var currentIndex = 0; currentIndex < current.length; currentIndex++) {
        try {
          var uuid = uuidFromHeaders(current[currentIndex].allHeaders());
          if (!baseline[uuid] &&
              sameGuard(routingKey(current[currentIndex]), expectedRoute)) {
            matches.push(current[currentIndex]);
          }
        } catch (error) {
          continue;
        }
      }
      if (matches.length === 1) {
        return draftRow(matches[0], account, drafts);
      }
      if (matches.length > 1) {
        throw new Error("More than one new Reply All review draft matched the source");
      }
    }
    throw new Error("Apple Mail did not persist the Reply All review draft");
  } catch (error) {
    if (nativeReply) {
      try {
        app.delete(nativeReply);
      } catch (ignored) {}
    }
    if (reviewDraft) {
      try {
        app.delete(reviewDraft);
      } catch (ignored) {}
    }
    throw error;
  }
}

function inspectDraft(app, payload) {
  var account = accountByName(app, payload.providerAccount);
  var drafts = mailboxByRole(account, "drafts");
  var message = findDraft(drafts, payload.uuid);
  if (!message) {
    throw new Error("Apple Mail draft not found: " + payload.uuid);
  }
  // Save only a uniquely matching compose buffer. Saving every outgoing
  // message can resurrect an unrelated draft that the user already discarded.
  var outgoing = outgoingForRoute(app, routingKey(message));
  if (outgoing) {
    app.save(outgoing);
    delay(0.3);
    message = findDraft(drafts, payload.uuid);
    if (!message) {
      throw new Error("Apple Mail draft disappeared while saving");
    }
  }
  return draftRow(message, account, drafts);
}

function openDraft(app, payload) {
  var account = accountByName(app, payload.providerAccount);
  var drafts = mailboxByRole(account, "drafts");
  var message = findDraft(drafts, payload.uuid);
  if (!message) {
    throw new Error("Apple Mail draft not found: " + payload.uuid);
  }
  var expected = guardForMessage(message);
  app.open(message);
  delay(0.4);
  var outgoing = outgoingForGuard(app, expected);
  if (!outgoing) {
    throw new Error("Apple Mail did not open the draft editor");
  }
  try {
    outgoing.visible.set(true);
  } catch (error) {
    try {
      outgoing.visible = true;
    } catch (ignored) {}
  }
  app.activate();
  return draftRow(message, account, drafts);
}

function sendDraft(app, payload) {
  var account = accountByName(app, payload.providerAccount);
  var drafts = mailboxByRole(account, "drafts");
  var message = findDraft(drafts, payload.uuid);
  if (!message) {
    throw new Error("Apple Mail draft not found: " + payload.uuid);
  }
  var liveGuard = guardForMessage(message);
  if (!sameGuard(liveGuard, payload.expectedGuard)) {
    throw new Error("draft_changed");
  }
  var outgoing = outgoingForGuard(app, payload.expectedGuard);
  if (!outgoing) {
    app.open(message);
    delay(0.4);
    outgoing = outgoingForGuard(app, payload.expectedGuard);
  }
  if (!outgoing) {
    throw new Error("Approved Apple Mail outgoing message not found");
  }
  var messageId = String(message.messageId() || "");
  var ok = app.send(outgoing);
  if (!ok) {
    throw new Error("Apple Mail rejected the send command");
  }
  return {
    messageId: messageId,
    uuid: payload.uuid
  };
}

function findSent(app, payload) {
  var account = accountByName(app, payload.providerAccount);
  var sent = mailboxByRole(account, "sent");
  var messages = sent.messages();
  var wantedMessageId = normalizedMessageId(payload.messageId);
  var limit = Math.min(messages.length, 250);
  for (var i = 0; i < limit; i++) {
    try {
      var messageId = String(messages[i].messageId() || "");
      var uuid = uuidFromHeaders(messages[i].allHeaders());
      if (
        (wantedMessageId && normalizedMessageId(messageId) === wantedMessageId) ||
        (payload.uuid && uuid === String(payload.uuid))
      ) {
        return {
          account: String(account.name()),
          mailbox: String(sent.name()),
          id: Number(messages[i].id()),
          messageId: messageId,
          uuid: uuid,
          subject: String(messages[i].subject() || ""),
          dateSent: String(messages[i].dateSent() || "")
        };
      }
    } catch (error) {
      continue;
    }
  }
  return null;
}

function discardDraft(app, payload) {
  var account = accountByName(app, payload.providerAccount);
  var drafts = mailboxByRole(account, "drafts");
  var message = findDraft(drafts, payload.uuid);
  if (!message) {
    return {deleted: false};
  }
  app.delete(message);
  return {deleted: true};
}

function run(argv) {
  var action = argv[0];
  var payload = JSON.parse(argv[1] || "{}");
  var app = Application("Mail");
  var result;
  if (action === "create") {
    result = createDraft(app, payload);
  } else if (action === "replyAll") {
    result = replyAllDraft(app, payload);
  } else if (action === "resolveAccount") {
    result = {providerAccount: String(accountForSender(app, payload.sender).name())};
  } else if (action === "inspect") {
    result = inspectDraft(app, payload);
  } else if (action === "open") {
    result = openDraft(app, payload);
  } else if (action === "send") {
    result = sendDraft(app, payload);
  } else if (action === "findSent") {
    result = findSent(app, payload);
  } else if (action === "discard") {
    result = discardDraft(app, payload);
  } else {
    throw new Error("Unsupported Apple Mail action: " + action);
  }
  return JSON.stringify(result);
}
"""


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag.casefold() == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"div", "p", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        value = "".join(self.parts).replace("\xa0", " ")
        value = re.sub(r"\n{3,}", "\n\n", value)
        return normalize_body(value).strip("\n")


def _message_from_source(source: str) -> Message | None:
    if not source:
        return None
    return BytesParser(policy=policy.default).parsebytes(source.encode("utf-8"))


def _body_from_source(source: str, fallback: str) -> str:
    message = _message_from_source(source)
    if message is None:
        return normalize_body(fallback).strip("\n")

    plain_parts = [
        part
        for part in message.walk()
        if part.get_content_type() == "text/plain"
        and not part.get_filename()
        and str(part.get_content()).strip()
    ]
    if plain_parts:
        return normalize_body(str(plain_parts[0].get_content())).strip("\n")

    html_parts = [
        part
        for part in message.walk()
        if part.get_content_type() == "text/html" and not part.get_filename()
    ]
    if html_parts:
        parser = _HTMLTextExtractor()
        parser.feed(str(html_parts[0].get_content()))
        html_body = parser.text()
        if html_body:
            return html_body
    return normalize_body(fallback).strip("\n")


def _attachments_from_source(source: str) -> list[Attachment]:
    message = _message_from_source(source)
    attachments: list[Attachment] = []
    if message is None:
        return attachments
    for part in message.walk():
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if not filename and disposition != "attachment":
            continue
        payload = part.get_payload(decode=True) or b""
        attachments.append(
            Attachment(
                name=str(filename or ""),
                mime_type=part.get_content_type(),
                size=len(payload),
                content_sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    return attachments


def _recipients(raw: object) -> list[Recipient]:
    if not isinstance(raw, list):
        return []
    return [
        Recipient(
            address=str(item.get("address") or ""),
            name=str(item.get("name") or ""),
        )
        for item in raw
        if isinstance(item, dict)
    ]


def _snapshot(raw: dict[str, Any]) -> DraftSnapshot:
    source = str(raw.get("source") or "")
    attachments = _attachments_from_source(source)
    provider_items = [
        item for item in raw.get("attachments") or [] if isinstance(item, dict)
    ]
    if provider_items:
        attachments = [
            Attachment(
                name=item.name,
                mime_type=item.mime_type,
                size=item.size,
                content_sha256=item.content_sha256,
                provider_id=next(
                    (
                        str(provider.get("id") or "")
                        for provider in provider_items
                        if str(provider.get("name") or "") == item.name
                    ),
                    "",
                ),
            )
            for item in attachments
        ]
    return DraftSnapshot(
        provider_account=str(raw.get("providerAccount") or ""),
        provider_uuid=str(raw.get("uuid") or ""),
        provider_message_id=str(raw.get("messageId") or ""),
        sender=str(raw.get("sender") or ""),
        to=_recipients(raw.get("to")),
        cc=_recipients(raw.get("cc")),
        bcc=_recipients(raw.get("bcc")),
        subject=str(raw.get("subject") or ""),
        body_text=_body_from_source(source, str(raw.get("content") or "")),
        attachments=attachments,
        provider_guard=dict(raw.get("guard") or {}),
    )


Runner = Callable[[str, dict[str, Any]], object]


class AppleMailProvider:
    def __init__(self, runner: Runner | None = None):
        self._runner = runner or self._run_jxa

    @staticmethod
    def _run_jxa(action: str, payload: dict[str, Any]) -> object:
        result = subprocess.run(
            [
                "osascript",
                "-l",
                "JavaScript",
                "-e",
                JXA_SOURCE,
                action,
                json.dumps(payload, ensure_ascii=False),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise AppleMailError(detail or f"Apple Mail 操作失败: {action}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AppleMailError("Apple Mail 返回了无效 JSON") from exc

    def create(
        self,
        *,
        sender: str,
        to: list[Recipient],
        cc: list[Recipient],
        bcc: list[Recipient],
        subject: str,
        body_text: str,
        attachments: list[Path],
        visible: bool,
    ) -> DraftSnapshot:
        raw = self._runner(
            "create",
            {
                "sender": sender,
                "to": [item.__dict__ for item in to],
                "cc": [item.__dict__ for item in cc],
                "bcc": [item.__dict__ for item in bcc],
                "subject": subject,
                "bodyText": body_text,
                "attachments": [str(path.resolve()) for path in attachments],
                "visible": visible,
            },
        )
        if not isinstance(raw, dict):
            raise AppleMailError("Apple Mail 未返回草稿对象")
        return _snapshot(raw)

    def reply_all(
        self,
        *,
        sender: str,
        provider_account: str,
        source_message_id: int,
        mailbox_path: str,
        body_text: str,
        attachments: list[Path],
        visible: bool,
    ) -> DraftSnapshot:
        payload = {
            "sender": sender,
            "providerAccount": provider_account,
            "sourceMessageId": source_message_id,
            "mailboxPath": mailbox_path,
            "bodyText": body_text,
            "attachments": [str(path.resolve()) for path in attachments],
            "visible": visible,
        }
        raw = self._runner("replyAll", payload)
        if not isinstance(raw, dict):
            raise AppleMailError("Apple Mail 未返回 Reply All 草稿对象")
        return _snapshot(raw)

    def resolve_account(self, sender: str) -> str:
        raw = self._runner("resolveAccount", {"sender": sender})
        if not isinstance(raw, dict) or not raw.get("providerAccount"):
            raise AppleMailError(f"无法为 {sender} 确定 Apple Mail 账户")
        return str(raw["providerAccount"])

    def inspect(self, *, provider_account: str, provider_uuid: str) -> DraftSnapshot:
        raw = self._runner(
            "inspect",
            {"providerAccount": provider_account, "uuid": provider_uuid},
        )
        if not isinstance(raw, dict):
            raise AppleMailError("Apple Mail 未返回草稿对象")
        return _snapshot(raw)

    def open(self, *, provider_account: str, provider_uuid: str) -> DraftSnapshot:
        raw = self._runner(
            "open",
            {"providerAccount": provider_account, "uuid": provider_uuid},
        )
        if not isinstance(raw, dict):
            raise AppleMailError("Apple Mail 未返回草稿对象")
        return _snapshot(raw)

    def send(self, snapshot: DraftSnapshot) -> SendStart:
        raw = self._runner(
            "send",
            {
                "providerAccount": snapshot.provider_account,
                "uuid": snapshot.provider_uuid,
                "expectedGuard": snapshot.provider_guard,
            },
        )
        if not isinstance(raw, dict):
            raise AppleMailError("Apple Mail 未返回发送起始凭证")
        return SendStart(
            provider_message_id=str(raw.get("messageId") or ""),
            provider_uuid=str(raw.get("uuid") or snapshot.provider_uuid),
        )

    def find_sent(
        self,
        *,
        provider_account: str,
        provider_uuid: str,
        provider_message_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0, timeout_seconds)
        while True:
            raw = self._runner(
                "findSent",
                {
                    "providerAccount": provider_account,
                    "uuid": provider_uuid,
                    "messageId": provider_message_id,
                },
            )
            if raw is not None:
                if not isinstance(raw, dict):
                    raise AppleMailError("Apple Mail 返回了无效 Sent 凭证")
                return {
                    "provider": "apple-mail",
                    "account": str(raw.get("account") or ""),
                    "mailbox": str(raw.get("mailbox") or ""),
                    "id": raw.get("id"),
                    "message_id": str(raw.get("messageId") or ""),
                    "provider_uuid": str(raw.get("uuid") or ""),
                    "subject": str(raw.get("subject") or ""),
                    "date_sent": str(raw.get("dateSent") or ""),
                }
            if time.monotonic() >= deadline:
                return None
            time.sleep(1)

    def discard(self, *, provider_account: str, provider_uuid: str) -> bool:
        raw = self._runner(
            "discard",
            {"providerAccount": provider_account, "uuid": provider_uuid},
        )
        return bool(isinstance(raw, dict) and raw.get("deleted"))
