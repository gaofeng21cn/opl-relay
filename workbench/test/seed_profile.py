import hashlib
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins" / "opl-relay" / "runtime"))
from codex_mail_workbench.store import connect_email_store, upsert_email_message


profile = Path(sys.argv[1]).resolve()
state = profile / "data" / "relay"
state.mkdir(parents=True, exist_ok=True)
templates = profile / "templates"
templates.mkdir(exist_ok=True)
(templates / "mail-signature.txt").write_text("Best regards,\nTest Owner\n", encoding="utf-8")
(state / "accounts.toml").write_text('''[[accounts]]
account_id = "demo"
email = "owner@example.test"
[accounts.imap]
host = "imap.example.test"
port = 993
security = "ssl"
username = "owner@example.test"
credential_ref = "unused-controlled-fixture"
''')
conn = connect_email_store(state / "mail.sqlite")
for index, (sender, subject, body) in enumerate([
    ("Conference secretary <secretary@example.test>", "GZDDW · Updated keynote programme", "Dear Professor,\n\nThe updated main auditorium programme is ready for your review.\n\nYour lecture is scheduled for Saturday, 08:50–09:10.\n\nBest regards,\nConference secretary\n\nPrevious correspondence remains in this thread."),
    ("Editorial office <editor@example.test>", "Revision received — colorectal cancer research", "The revised manuscript and point-by-point response have been received. Thank you for your contribution."),
    ("Research collaborator <collaborator@example.test>", "Planning our next research discussion", "Thank you for sharing the preliminary analysis. Shall we discuss the validation plan next week?"),
], 1):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "Test Owner <owner@example.test>"
    message["Cc"] = "Professor <professor@example.test>"
    message["Subject"] = subject
    message["Message-ID"] = f"<controlled-{index}@example.test>"
    message["Date"] = f"Thu, 05 Nov 2026 0{9-index}:00:00 +0800"
    message.set_content(body)
    raw = message.as_bytes()
    upsert_email_message(conn, account_id="demo", folder="INBOX", folder_slug="INBOX", uid=index,
                         uidvalidity=1, message_id=str(message["Message-ID"]), subject=subject,
                         sender=sender, recipient=str(message["To"]), date_iso=f"2026-11-05T0{9-index}:00:00+08:00",
                         raw_sha256=hashlib.sha256(raw).hexdigest(), raw_eml=raw, attachments=[],
                         ingest_ts="2026-11-05T08:01:00+08:00")
conn.close()
print(profile)
