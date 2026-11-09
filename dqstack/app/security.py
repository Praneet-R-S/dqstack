"""Passwords, sessions, CSRF and lockout (section 15).

Passwords use Argon2id, the current recommendation for password storage. Session
cookies carry a random token and the database keeps only its SHA-256, so a stolen
database backup does not contain anyone's live session. Every state-changing request
must also present the session's CSRF token in a header, which a cross-site form
cannot do.

Lockout: five failed attempts lock the account for fifteen minutes (section 13,
"wrong password repeatedly"). The counter resets on success. The message for a wrong
password and for an unknown email is identical, so the sign-in form cannot be used
to discover who has an account.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

try:  # argon2-cffi >= 23.1 renamed InvalidHash; the old name remains as an alias
    from argon2.exceptions import InvalidHashError
except ImportError:  # pragma: no cover - older argon2-cffi
    from argon2.exceptions import InvalidHash as InvalidHashError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import utcnow
from .models import AuthSession, Person

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 10


class AuthError(Exception):
    """Raised with a message that is safe to show the person signing in."""


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(stored: str | None, raw: str) -> bool:
    if not stored:
        return False
    try:
        return _hasher.verify(stored, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problem(raw: str) -> str | None:
    """A plain-language reason a password is refused, or None if it is acceptable."""
    if len(raw) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if raw.lower() in {"password123", "1234567890", "qwertyuiop", "dqstack123"}:
        return "That password is too common. Choose something less guessable."
    return None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes even for timezone columns; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


GENERIC_FAILURE = "That email or phone and password do not match."


def authenticate(db: Session, login: str, password: str) -> Person:
    """Check a sign-in attempt, applying the lockout rule."""
    login = (login or "").strip().lower()
    person = db.scalar(select(Person).where((Person.email == login) | (Person.phone == login)))
    if person is None and "@" not in login:
        # A phone number written another way (98765 43210, +91 98765-43210 ...).
        from .signin import find_by_phone
        person = find_by_phone(db, login)
    now = utcnow()
    if person is None:
        # Do the same amount of hashing work as a real check, so response time does
        # not reveal whether the account exists.
        verify_password(_DUMMY_HASH, password)
        raise AuthError(GENERIC_FAILURE)
    if (locked := lock_message(person)):
        raise AuthError(locked)
    if person.status == "disabled":
        raise AuthError("This account has been disabled. Ask your administrator.")
    if not verify_password(person.password_hash, password):
        count_failure(person)
        db.commit()
        raise AuthError(GENERIC_FAILURE)
    if person.status == "invited":
        raise AuthError("Finish setting up your account from the invitation link first.")
    person.failed_attempts = 0
    person.locked_until = None
    person.last_sign_in = now
    db.commit()
    return person


_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


def count_failure(person: Person) -> None:
    """One failed sign-in towards the lockout: a wrong password, or a sign-in code
    that died after five wrong tries (signin.py)."""
    person.failed_attempts = (person.failed_attempts or 0) + 1
    if person.failed_attempts >= settings.lockout_failures:
        person.locked_until = utcnow() + timedelta(minutes=settings.lockout_minutes)
        person.failed_attempts = 0


def lock_message(person: Person) -> str | None:
    """The sentence for a locked account, or None if it is not locked."""
    now = utcnow()
    locked = _aware(person.locked_until)
    if not (locked and locked > now):
        return None
    minutes = max(1, int((locked - now).total_seconds() // 60) + 1)
    return (f"This account is locked after too many wrong passwords. "
            f"Try again in {minutes} minute{'s' if minutes != 1 else ''}, "
            "or ask an administrator to unlock it.")


def start_session(db: Session, person: Person, ip: str | None = None,
                  device: str | None = None) -> tuple[str, AuthSession]:
    token = new_token()
    sess = AuthSession(token_hash=token_hash(token), person_id=person.id,
                       company_id=person.company_id, csrf_token=secrets.token_urlsafe(24),
                       expires_at=utcnow() + timedelta(hours=settings.session_hours),
                       ip=ip, device=(device or "")[:255] or None)
    db.add(sess)
    db.commit()
    return token, sess


def session_for(db: Session, token: str | None) -> tuple[AuthSession, Person] | None:
    """The live session and person for a cookie token, or None.

    Disabled people and revoked or expired sessions return None on the very next
    request, which is what "signed out everywhere immediately" means in practice:
    there is no long-lived token on the device to outlast the decision."""
    if not token:
        return None
    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(token)))
    if sess is None or sess.revoked_at is not None:
        return None
    if _aware(sess.expires_at) <= utcnow():
        return None
    person = db.get(Person, sess.person_id)
    if person is None or person.status != "active":
        return None
    return sess, person


def revoke_all_sessions(db: Session, person_id: str) -> int:
    n = 0
    for s in db.scalars(select(AuthSession).where(AuthSession.person_id == person_id,
                                                   AuthSession.revoked_at.is_(None))):
        s.revoked_at = utcnow()
        n += 1
    return n


def csrf_ok(sess: AuthSession, header_value: str | None) -> bool:
    return bool(header_value) and hmac.compare_digest(sess.csrf_token, header_value or "")


def hash_api_key(raw: str) -> str:
    return token_hash(raw)


def new_api_key() -> tuple[str, str]:
    """Returns (full key shown once, display prefix)."""
    raw = "dqk_" + secrets.token_urlsafe(32)
    return raw, raw[:12]
