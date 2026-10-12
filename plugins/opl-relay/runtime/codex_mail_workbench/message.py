from __future__ import annotations

from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser


class QuoteText(HTMLParser):
    """Readable HTML projection; never execute markup or retain CSS/scripts."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.suppressed += 1
        elif not self.suppressed and tag in {"br", "p", "div", "li", "tr", "blockquote"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.suppressed = max(0, self.suppressed - 1)
        elif not self.suppressed and tag in {"p", "div", "li", "tr", "blockquote"}:
            self.parts.append("\n")
        elif not self.suppressed and tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def html_to_text(content):
    parser = QuoteText()
    parser.feed(content)
    # Formatting whitespace is not mail prose. Keep each paragraph/table row
    # and all visible words; the unchanged MIME remains the original evidence.
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def decode_header_text(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def parse_message_date(raw: bytes) -> str:
    msg = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    hdr = msg.get("Date")
    if not hdr:
        return ""
    try:
        return parsedate_to_datetime(hdr).astimezone().isoformat()
    except Exception:
        return ""


def parse_address_list(raw: str | None) -> list[dict[str, str]]:
    if not raw:
        return []
    decoded = decode_header_text(raw)
    return [
        {"name": name.strip(), "address": address.strip()}
        for name, address in getaddresses([decoded])
        if address.strip()
    ]


def parse_headers(raw: bytes) -> dict[str, object]:
    msg = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    return {
        "message_id": str(msg.get("Message-Id") or "").strip(),
        "subject": decode_header_text(msg.get("Subject")),
        "from": decode_header_text(msg.get("From")),
        "to": decode_header_text(msg.get("To")),
        "cc": decode_header_text(msg.get("Cc")),
        "bcc": decode_header_text(msg.get("Bcc")),
        "date": parse_message_date(raw),
        "from_addresses": parse_address_list(msg.get("From")),
        "to_addresses": parse_address_list(msg.get("To")),
        "cc_addresses": parse_address_list(msg.get("Cc")),
        "bcc_addresses": parse_address_list(msg.get("Bcc")),
    }


def extract_text_body(raw: bytes) -> str:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    if isinstance(msg, EmailMessage):
        body = msg.get_body(preferencelist=("plain", "html"))
        if body is not None:
            try:
                content = body.get_content()
            except Exception:
                content = ""
            text = str(content or "")
            return html_to_text(text) if body.get_content_type() == "text/html" else text
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            if part.get_content_type() != "text/plain":
                continue
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    payload_raw = msg.get_payload()
    return payload_raw if isinstance(payload_raw, str) else ""


def extract_attachments(raw: bytes) -> list[dict[str, object]]:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    out: list[dict[str, object]] = []
    for idx, part in enumerate(msg.walk(), start=1):
        if part.is_multipart():
            continue
        filename = part.get_filename() or ""
        disposition = (part.get_content_disposition() or "").lower()
        if disposition != "attachment" and not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        if not isinstance(payload, bytes):
            payload = str(payload).encode("utf-8", errors="ignore")
        out.append(
            {
                "name": filename or f"part-{idx}.bin",
                "size": len(payload),
                "content_type": part.get_content_type(),
            }
        )
    return out
