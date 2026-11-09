"""Outgoing WhatsApp messages through the WhatsApp Business Cloud API (Meta's Graph API):
purchase orders to suppliers, alerts to staff and receipts to customers.

Set up with

    DQ_WHATSAPP_TOKEN            a permanent system-user access token
    DQ_WHATSAPP_PHONE_NUMBER_ID  the id of the business phone number (not the number)
    DQ_WHATSAPP_API_VERSION      optional, default v21.0

Unset, this is development: each message is written to the log and kept in `outbox`,
nothing leaves the server, and the screens fall back to a wa.me link the person sends
from their own phone. Set, the server sends.

WhatsApp only lets a business start a conversation with a template Meta has approved;
free text is delivered only within 24 hours of the other person's last message to
the business. Name the approved templates with

    DQ_WHATSAPP_TEMPLATE_PO       header: document (the PDF). body: {{1}} order number,
                                  {{2}} who is ordering, {{3}} total
    DQ_WHATSAPP_TEMPLATE_ALERT    body: {{1}} person's name, {{2}} the alert(s)
    DQ_WHATSAPP_TEMPLATE_RECEIPT  body: {{1}} store, {{2}} bill number, {{3}} date and
                                  time, {{4}} items, {{5}} total and payment
    DQ_WHATSAPP_LANGUAGE          the templates' language code, default en

Without a template name the message goes as free text (or a document with a caption),
which WhatsApp delivers only inside that 24-hour window.

Like the mailer and sms.py, a failure is raised (WhatsAppError) with a plain message,
never silently dropped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

from .sms import normalise

log = logging.getLogger("dqstack.whatsapp")

GRAPH = "https://graph.facebook.com"
DEFAULT_VERSION = "v21.0"


@dataclass
class Sent:
    to: str                                   # +91... as normalised
    text: str                                 # what the person reads (or the params)
    payload: dict = field(default_factory=dict)
    document: tuple[str, bytes, str] | None = None   # filename, data, mime


outbox: list[Sent] = []


class WhatsAppError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def configured() -> bool:
    return bool(_env("DQ_WHATSAPP_TOKEN") and _env("DQ_WHATSAPP_PHONE_NUMBER_ID"))


def template(kind: str) -> str | None:
    """The approved template for 'po', 'alert' or 'receipt', if one is named."""
    return _env(f"DQ_WHATSAPP_TEMPLATE_{kind.upper()}") or None


def _base() -> str:
    version = _env("DQ_WHATSAPP_API_VERSION", DEFAULT_VERSION)
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise WhatsAppError(f"DQ_WHATSAPP_API_VERSION must look like {DEFAULT_VERSION}.")
    return f"{GRAPH}/{version}/{_env('DQ_WHATSAPP_PHONE_NUMBER_ID')}"


def _number(to: str | None) -> str:
    n = normalise(to)
    if n is None:
        raise WhatsAppError(f"{to or 'That'} is not a mobile number WhatsApp can be sent to.")
    return n


def one_line(text: str, limit: int = 900) -> str:
    """Template parameters may not hold new lines, tabs or runs of spaces."""
    s = re.sub(r"\s*\n\s*", "; ", text.strip())
    s = re.sub(r"[\t ]{2,}", " ", s.replace("\t", " "))
    return s if len(s) <= limit else s[:limit - 1] + "…"


# Graph API error codes worth a plainer sentence than Meta's own.
_PLAIN = {
    131047: "more than 24 hours have passed since they last wrote to the business, so "
            "WhatsApp needs an approved template for this message",
    131026: "that number is not on WhatsApp or cannot receive messages",
    132000: "the template's number of blanks does not match what was sent",
    132001: "the template name or language is not approved on the WhatsApp account",
    190: "the WhatsApp access token has expired or is wrong",
    131030: "that number is not on the WhatsApp test account's allowed list",
}


def _request(req: urllib.request.Request, number: str) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (fixed https host)
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            err = json.loads(e.read() or b"{}").get("error", {})
            detail = _PLAIN.get(err.get("code")) or err.get("message") or ""
        except (ValueError, AttributeError, OSError):
            pass
        raise WhatsAppError(f"WhatsApp did not take the message to {number}"
                            + (f": {detail}." if detail else f" (status {e.code}).")) from None
    except (urllib.error.URLError, OSError) as e:
        raise WhatsAppError(f"The WhatsApp message to {number} could not be sent "
                            f"({e.__class__.__name__}).") from e
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise WhatsAppError(f"WhatsApp gave an answer that could not be read for {number}.") \
            from None


def _auth() -> dict:
    return {"Authorization": "Bearer " + _env("DQ_WHATSAPP_TOKEN")}


def _post(number: str, payload: dict) -> dict:
    body = {"messaging_product": "whatsapp", "recipient_type": "individual",
            "to": number.lstrip("+"), **payload}
    req = urllib.request.Request(_base() + "/messages", data=json.dumps(body).encode(),
                                 method="POST",
                                 headers={**_auth(), "Content-Type": "application/json"})
    out = _request(req, number)
    if not out.get("messages"):
        raise WhatsAppError(f"WhatsApp did not confirm the message to {number}.")
    return out


def upload_media(data: bytes, filename: str, mime: str = "application/pdf",
                 number: str = "the supplier") -> str:
    """Upload a file to the business number's media store; returns the media id."""
    boundary = "dq" + uuid.uuid4().hex
    safe = re.sub(r'["\r\n\\]', "_", filename)
    parts = []
    for name, value in (("messaging_product", "whatsapp"), ("type", mime)):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                     f"{value}\r\n".encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f"filename=\"{safe}\"\r\nContent-Type: {mime}\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(_base() + "/media", data=b"".join(parts), method="POST",
                                 headers={**_auth(),
                                          "Content-Type": f"multipart/form-data; boundary={boundary}"})
    media_id = _request(req, number).get("id")
    if not media_id:
        raise WhatsAppError(f"WhatsApp did not accept the file {filename}.")
    return str(media_id)


def _template_payload(name: str, params: list[str], header: dict | None = None) -> dict:
    components = []
    if header:
        components.append({"type": "header", "parameters": [header]})
    if params:
        components.append({"type": "body", "parameters": [
            {"type": "text", "text": one_line(str(p))} for p in params]})
    return {"type": "template", "template": {
        "name": name, "language": {"code": _env("DQ_WHATSAPP_LANGUAGE", "en")},
        "components": components}}


def send_text(to: str, text: str) -> None:
    """Free text: delivered only within 24 hours of the person's last message."""
    number = _number(to)
    payload = {"type": "text", "text": {"preview_url": False, "body": text[:4096]}}
    outbox.append(Sent(number, text, payload))
    if not configured():
        log.info("whatsapp (not sent: not configured) to=%s\n%s", number, text)
        return
    _post(number, payload)


def send_template(to: str, name: str, params: list[str], header: dict | None = None) -> None:
    number = _number(to)
    payload = _template_payload(name, params, header)
    outbox.append(Sent(number, " | ".join(one_line(str(p)) for p in params), payload))
    if not configured():
        log.info("whatsapp template %s (not sent: not configured) to=%s %s", name, number, params)
        return
    _post(number, payload)


def send(to: str, text: str, kind: str, params: list[str]) -> None:
    """The approved template for `kind` with `params` when one is named, else `text`."""
    name = template(kind)
    if name:
        send_template(to, name, params)
    else:
        send_text(to, text)


def send_document(to: str, data: bytes, filename: str, caption: str, kind: str = "po",
                  params: list[str] | None = None, mime: str = "application/pdf") -> None:
    """Upload the file, then send it: inside the approved template for `kind` (the file
    as its header) when one is named, else as a document with a caption."""
    number = _number(to)
    name = template(kind)
    media_id = upload_media(data, filename, mime, number) if configured() else "dev-media"
    doc = {"id": media_id, "filename": filename}
    if name:
        payload = _template_payload(name, params or [], {"type": "document", "document": doc})
    else:
        payload = {"type": "document", "document": {**doc, "caption": caption[:1024]}}
    outbox.append(Sent(number, caption, payload, (filename, data, mime)))
    if not configured():
        log.info("whatsapp document %s (not sent: not configured) to=%s\n%s",
                 filename, number, caption)
        return
    _post(number, payload)
