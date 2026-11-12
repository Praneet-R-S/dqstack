"""Credentials at rest: Zoho client secrets and tokens, encrypted in the database.

Nothing else in the application stores a third party's secret in the database (the
WhatsApp, SMS and Google/Microsoft keys are the server's own, read from the
environment), so connectors bring their own: Fernet (AES-128-CBC with an
HMAC-SHA256 tag, from the `cryptography` package already installed for signed
tokens), keyed by HKDF-SHA256 from DQ_SECRET_KEY with a label of its own, so the
key that signs sessions is never used directly to encrypt.

Consequences an operator must know (deploy/README.md, docs/SECURITY.md):

* A copy of the database alone does not reveal a token; the secret key is needed too.
* Changing DQ_SECRET_KEY makes stored credentials unreadable. Nothing breaks: each
  connector shows "connect again" and an Admin repeats the Zoho consent. Rotation is
  therefore possible, at the price of reconnecting.
"""

from __future__ import annotations

import base64
import json

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .. import config

_LABEL = b"dqstack connector credentials v1"


def _fernet() -> Fernet:
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_LABEL).derive(
        config.settings.secret_key.encode())
    return Fernet(base64.urlsafe_b64encode(raw))


def seal(values: dict) -> bytes:
    """Encrypt a small dict of secrets for the `credentials` column."""
    return _fernet().encrypt(json.dumps(values, sort_keys=True).encode())


def unseal(blob: bytes | None) -> dict | None:
    """The secrets, or None when there are none or they cannot be read (the secret
    key changed, or the bytes were altered: Fernet's tag catches both)."""
    if not blob:
        return None
    try:
        return json.loads(_fernet().decrypt(bytes(blob)))
    except (InvalidToken, ValueError):
        return None
