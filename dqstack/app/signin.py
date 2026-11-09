"""Sign-in extras: one-time codes and the company's sign-in security setting.

One-time codes are six digits, sent by text message (sms.py) or email (mailer.py),
for two things:

    reset    "forgot password" by phone: the code, with the phone number, lets the
             person choose a new password.
    signin   the optional second step after a correct password, when the company
             turns it on for Owners and Admins or for everyone.

Rules for both: a code works for 10 minutes; five wrong tries and it is dead; only a
keyed hash (HMAC with DQ_SECRET_KEY) is stored, so a copy of the database does not
reveal live codes and a six-digit code cannot be found by hashing every candidate.
A sign-in code may be sent again at most twice (three codes in all); each new code
replaces the old one and gets its own five tries.

Text messages are used when a provider is configured (DQ_SMS_PROVIDER, see sms.py)
and, in development and tests, also without one -- the message then lands in the log
and `sms.outbox`. A production server without a text provider sends sign-in codes by
email and does not offer reset by phone, rather than "sending" codes into its log.
"""

from __future__ import annotations

import re

import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from sqlalchemy import or_, select

from . import mailer, sms
from .config import settings
from .db import utcnow
from .models import Company, OneTimeCode, Person
from .security import _aware

log = logging.getLogger("dqstack.signin")

CODE_MINUTES = 10
MAX_TRIES = 5
MAX_SENDS = 3
TWO_STEP_CHOICES = ("off", "admins", "everyone")
ADMIN_ROLES = ("owner", "admin")


class SendFailed(RuntimeError):
    pass


# ------------------------------------------------------------------ company setting

GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def company_settings(company: Company | None) -> dict:
    raw = (company.signin if company is not None else None) or {}
    two = raw.get("two_step")
    tenant = raw.get("microsoft_tenant")
    return {"two_step": two if two in TWO_STEP_CHOICES else "off",
            "idp": bool(raw.get("idp", True)),
            "microsoft_tenant": tenant if isinstance(tenant, str) and GUID.fullmatch(tenant) else None}


def two_step_applies(company: Company | None, person: Person) -> bool:
    mode = company_settings(company)["two_step"]
    return mode == "everyone" or (mode == "admins" and person.role in ADMIN_ROLES)


def texts_available() -> bool:
    """Whether a code can go by text message from this server."""
    return sms.configured() or settings.env in ("development", "test")


# ------------------------------------------------------------------ codes

def new_code() -> str:
    return f"{secrets.randbelow(10 ** 6):06d}"


def code_hash(person_id: str, purpose: str, code: str) -> str:
    msg = f"one-time-code:{purpose}:{person_id}:{code}".encode()
    return hmac.new(settings.secret_key.encode(), msg, hashlib.sha256).hexdigest()


def clean(code: str | None) -> str:
    return "".join(ch for ch in (code or "") if ch.isdigit())


def is_live(row: OneTimeCode | None) -> bool:
    return (row is not None and row.used_at is None and row.attempts < MAX_TRIES
            and _aware(row.expires_at) > utcnow())


def check(row: OneTimeCode | None, code: str | None) -> bool:
    """True and marks the code used if it is right; otherwise counts a try. A dead or
    expired code is never right, so the sixth try fails even with the right code."""
    if not is_live(row):
        return False
    given = clean(code)
    if len(given) == 6 and hmac.compare_digest(row.code_hash,
                                               code_hash(row.person_id, row.purpose, given)):
        row.used_at = utcnow()
        return True
    row.attempts += 1
    return False


def tries_left(row: OneTimeCode) -> int:
    return max(0, MAX_TRIES - row.attempts)


def issue(db, person: Person, purpose: str, channel: str,
          ticket_hash: str | None = None) -> tuple[OneTimeCode, str]:
    """A fresh code for `person`. Earlier live codes for the same purpose stop working,
    so only the newest message counts."""
    now = utcnow()
    for old in db.scalars(select(OneTimeCode).where(OneTimeCode.person_id == person.id,
                                                    OneTimeCode.purpose == purpose,
                                                    OneTimeCode.used_at.is_(None))):
        old.used_at = now
    code = new_code()
    row = OneTimeCode(company_id=person.company_id, person_id=person.id, purpose=purpose,
                      channel=channel, code_hash=code_hash(person.id, purpose, code),
                      ticket_hash=ticket_hash, expires_at=now + timedelta(minutes=CODE_MINUTES))
    db.add(row)
    db.flush()
    return row, code


def renew(row: OneTimeCode) -> str:
    """Replace the code on a pending sign-in (the "Send a new code" button)."""
    code = new_code()
    row.code_hash = code_hash(row.person_id, row.purpose, code)
    row.attempts = 0
    row.sends += 1
    row.expires_at = utcnow() + timedelta(minutes=CODE_MINUTES)
    return code


def latest_reset(db, person: Person) -> OneTimeCode | None:
    return db.scalar(select(OneTimeCode).where(OneTimeCode.person_id == person.id,
                                               OneTimeCode.purpose == "reset",
                                               OneTimeCode.used_at.is_(None))
                     .order_by(OneTimeCode.created_at.desc()).limit(1))


# ------------------------------------------------------------------ people and delivery

def find_by_phone(db, phone: str | None) -> Person | None:
    """The one person whose phone is this number, however either was written. If two
    accounts carry the same number in different spellings, neither is chosen: a code
    must never reach an account the person did not mean."""
    n = sms.normalise(phone)
    if n is None:
        return None
    local = n[3:] if n.startswith("+91") else None
    forms = {n, phone.strip()} | ({local, "0" + local, "91" + local} if local else set())
    rows = db.scalars(select(Person).where(Person.phone.is_not(None), or_(
        Person.phone.in_(forms), Person.phone.like(f"%{n[-4:]}")))).all()
    found = {p.id: p for p in rows if sms.normalise(p.phone) == n}
    return next(iter(found.values())) if len(found) == 1 else None


def channel_for(person: Person) -> tuple[str, str] | None:
    """("sms", number) or ("email", address) for a sign-in code: the phone when a text
    can be sent, otherwise the email; None if the person has neither."""
    number = sms.normalise(person.phone)
    if number and texts_available():
        return "sms", number
    if person.email:
        return "email", person.email
    return None


def hint(channel: str, to: str) -> str:
    if channel == "sms":
        return f"your phone ending {to[-4:]}"
    user, _, domain = to.partition("@")
    return f"your email {user[:1]}***@{domain}"


TEXTS = {
    "signin": ("Your DQ-Stack sign-in code",
               "Your DQ-Stack sign-in code is {code}. It works for 10 minutes. "
               "Do not share it with anyone."),
    "reset": ("Your DQ-Stack password reset code",
              "Your DQ-Stack password reset code is {code}. It works for 10 minutes. "
              "If you did not ask for it, ignore this message."),
}


def deliver(channel: str, to: str, purpose: str, code: str, name: str = "") -> None:
    subject, text = TEXTS[purpose]
    text = text.format(code=code)
    try:
        if channel == "sms":
            sms.send(to, text)
        else:
            mailer.send(to, subject, f"Hello {name},\n\n{text}\n" if name else text)
    except (sms.SmsError, mailer.MailError) as e:
        log.warning("one-time code (%s) could not be sent by %s: %s", purpose, channel,
                    e.__class__.__name__)
        raise SendFailed(str(e)) from e
