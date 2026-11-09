"""Outgoing text messages (SMS): one-time codes, password resets and receipts.

Choose a provider with DQ_SMS_PROVIDER:

    (unset)   development: the message is written to the log and kept in `outbox`,
              which is what the tests read. Nothing leaves the server.
    twilio    Twilio's REST API. Needs DQ_TWILIO_ACCOUNT_SID, DQ_TWILIO_AUTH_TOKEN and
              DQ_SMS_FROM (a Twilio number or sender id).
    webhook   Any other gateway (MSG91, Gupshup, Exotel ...) through a small adapter:
              the server POSTs {"to": "+9198...", "text": "..."} as JSON to
              DQ_SMS_WEBHOOK_URL, with "Authorization: Bearer DQ_SMS_WEBHOOK_TOKEN".

Indian gateways need the message templates registered on DLT before they deliver;
the texts this module sends are fixed sentences with the code or amount filled in,
so each is one template.

Like the mailer, a failure is raised (SmsError) so the screen can say the message did
not go, never silently dropped.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("dqstack.sms")


@dataclass
class Sent:
    to: str
    text: str


outbox: list[Sent] = []


class SmsError(RuntimeError):
    pass


def normalise(phone: str | None) -> str | None:
    """+91 and ten digits for an Indian mobile written any usual way; other numbers
    must already carry their + country code. None if it cannot be a phone number."""
    if not phone:
        return None
    s = re.sub(r"[\s\-().]", "", phone.strip())
    if s.startswith("00"):
        s = "+" + s[2:]
    if re.fullmatch(r"(?:\+?91|0)?[6-9]\d{9}", s):
        return "+91" + s[-10:]
    if re.fullmatch(r"\+[1-9]\d{6,14}", s):
        return s
    return None


def configured() -> bool:
    return bool(os.environ.get("DQ_SMS_PROVIDER"))


def send(to: str, text: str) -> None:
    number = normalise(to)
    if number is None:
        raise SmsError(f"{to} is not a phone number a text can be sent to.")
    outbox.append(Sent(number, text))
    provider = (os.environ.get("DQ_SMS_PROVIDER") or "").strip().lower()
    if not provider:
        log.info("sms (not sent: no provider configured) to=%s\n%s", number, text)
        return
    if provider == "twilio":
        sid = os.environ.get("DQ_TWILIO_ACCOUNT_SID", "")
        token = os.environ.get("DQ_TWILIO_AUTH_TOKEN", "")
        sender = os.environ.get("DQ_SMS_FROM", "")
        if not (sid and token and sender):
            raise SmsError("Text messages are not set up: the Twilio account details are missing.")
        url = f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(sid)}/Messages.json"
        data = urllib.parse.urlencode({"To": number, "From": sender, "Body": text}).encode()
        auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded"})
    elif provider == "webhook":
        url = os.environ.get("DQ_SMS_WEBHOOK_URL", "")
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
            raise SmsError("Text messages are not set up: DQ_SMS_WEBHOOK_URL must be https.")
        headers = {"Content-Type": "application/json"}
        if os.environ.get("DQ_SMS_WEBHOOK_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["DQ_SMS_WEBHOOK_TOKEN"]
        req = urllib.request.Request(url, data=json.dumps({"to": number, "text": text}).encode(),
                                     method="POST", headers=headers)
    else:
        raise SmsError(f"Unknown text message provider {provider!r}.")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 (fixed https hosts)
            if r.status >= 300:
                raise SmsError(f"The text to {number} could not be sent (status {r.status}).")
    except (urllib.error.URLError, OSError) as e:
        raise SmsError(f"The text to {number} could not be sent ({e.__class__.__name__}).") from e
