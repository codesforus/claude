"""Email delivery over SMTP. Without SMTP configured, emails land in the outbox folder."""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from leadgen.settings import Settings

log = logging.getLogger(__name__)


@dataclass
class Attachment:
    filename: str
    data: bytes
    mimetype: str = "application/octet-stream"


@dataclass
class Email:
    subject: str
    html: str
    text: str
    attachments: list[Attachment] = field(default_factory=list)


class Mailer:
    def __init__(self, settings: Settings):
        self.s = settings

    def send(self, email: Email, to: list[str] | None = None, cc: list[str] | None = None) -> Path | None:
        """Send over SMTP; returns the outbox path instead when SMTP or recipients are missing."""
        to = to if to is not None else self.s.report_to
        cc = cc if cc is not None else self.s.report_cc
        msg = self._message(email, to, cc)
        if not self.s.smtp_host or not to:
            log.warning("SMTP nebo příjemci nejsou nastaveni, email jen uložen")
            return self._write(msg)

        context = ssl.create_default_context()
        if self.s.smtp_security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, context=context, timeout=30)
        else:
            server = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30)
        with server:
            if self.s.smtp_security == "starttls":
                server.starttls(context=context)
            if self.s.smtp_user:
                server.login(self.s.smtp_user, self.s.smtp_password)
            server.send_message(msg)
        log.info("email odeslán: %s -> %s", email.subject, msg["To"])
        return None

    def save_to_outbox(self, email: Email) -> Path:
        return self._write(self._message(email, self.s.report_to, self.s.report_cc))

    def _message(self, email: Email, to: list[str], cc: list[str]) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = email.subject
        msg["From"] = self.s.smtp_from or self.s.smtp_user or "leadgen@localhost"
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg.set_content(email.text)
        msg.add_alternative(email.html, subtype="html")
        for a in email.attachments:
            maintype, _, subtype = a.mimetype.partition("/")
            msg.add_attachment(a.data, maintype=maintype, subtype=subtype, filename=a.filename)
        return msg

    def _write(self, msg: EmailMessage) -> Path:
        outbox = Path(self.s.outbox_dir)
        outbox.mkdir(parents=True, exist_ok=True)
        ascii_subject = unicodedata.normalize("NFKD", msg["Subject"]).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_subject.lower())[:60].strip("-")
        path = outbox / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{slug}.eml"
        path.write_bytes(bytes(msg))
        log.info("email uložen do %s", path)
        return path
