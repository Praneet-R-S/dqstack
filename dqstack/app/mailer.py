"""Outgoing email: invitations, password resets, alerts and order sending.

With DQ_SMTP_HOST set, messages go out over SMTP with STARTTLS. Without it they are
written to the application log and kept in `outbox` -- the development default, and
what the tests read to find an invitation or reset link. A server never silently
drops a message: if sending fails, the caller is told so it can say so on screen.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage

from .config import settings

log = logging.getLogger("dqstack.mail")


@dataclass
class Sent:
    to: str
    subject: str
    body: str
    attachments: list[tuple[str, bytes, str]] = field(default_factory=list)


outbox: list[Sent] = []


class MailError(RuntimeError):
    pass


def send(to: str, subject: str, body: str,
         attachments: list[tuple[str, bytes, str]] | None = None) -> None:
    attachments = attachments or []
    outbox.append(Sent(to, subject, body, attachments))
    if not settings.smtp_host:
        log.info("mail (not sent: no SMTP host configured) to=%s subject=%s\n%s", to, subject, body)
        return
    msg = EmailMessage()
    msg["From"] = settings.mail_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    for name, data, mime in attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password or "")
            s.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise MailError(f"The email to {to} could not be sent ({e.__class__.__name__}).") from e
