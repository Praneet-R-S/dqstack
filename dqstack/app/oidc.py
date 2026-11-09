"""Google and Microsoft sign-in (OpenID Connect, authorization code flow with PKCE).

On only when the server has the provider's keys:

    DQ_GOOGLE_CLIENT_ID, DQ_GOOGLE_CLIENT_SECRET
    DQ_MICROSOFT_CLIENT_ID, DQ_MICROSOFT_CLIENT_SECRET, DQ_MICROSOFT_TENANT
        (tenant: "organizations" by default = any work or school account; "common";
         "consumers"; or one tenant id, a GUID, to admit only that organisation)

Register this redirect address with the provider:

    {DQ_PUBLIC_URL}/api/auth/oidc/{google|microsoft}/callback

The flow: /start makes a random `state`, `nonce` and PKCE verifier, puts them in a
short-lived cookie signed with DQ_SECRET_KEY (ten minutes, http-only, only sent to
/api/auth/oidc), and sends the browser to the provider. The callback refuses unless
the `state` it gets back matches the cookie, exchanges the code (with the verifier)
for an ID token, and checks the token's RS256 signature against the provider's
published keys, its issuer, audience, expiry and the cookie's nonce.

It only ever signs in an existing, active person whose email matches the verified
email. It never creates a company or a person.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt
from jwt import PyJWK

from . import config

log = logging.getLogger("dqstack.oidc")

PROVIDERS = ("google", "microsoft")
NAMES = {"google": "Google", "microsoft": "Microsoft"}
COOKIE = "dq_oidc"
COOKIE_PATH = "/api/auth/oidc"
COOKIE_SECONDS = 600
MS_CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")


class OidcError(Exception):
    """`reason` is a short code the sign-in screen turns into a sentence."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason


# ------------------------------------------------------------------ configuration

def client(provider: str) -> tuple[str, str] | None:
    s = config.settings
    pair = {"google": (s.google_client_id, s.google_client_secret),
            "microsoft": (s.microsoft_client_id, s.microsoft_client_secret)}.get(provider)
    return pair if pair and pair[0] and pair[1] else None


def configured(provider: str) -> bool:
    return client(provider) is not None


def enabled() -> dict[str, bool]:
    return {p: configured(p) for p in PROVIDERS}


def endpoints(provider: str) -> dict[str, str]:
    if provider == "google":
        return {"authorize": "https://accounts.google.com/o/oauth2/v2/auth",
                "token": "https://oauth2.googleapis.com/token",
                "jwks": "https://www.googleapis.com/oauth2/v3/certs"}
    tenant = urllib.parse.quote(config.settings.microsoft_tenant or "organizations", safe="")
    base = f"https://login.microsoftonline.com/{tenant}"
    return {"authorize": f"{base}/oauth2/v2.0/authorize",
            "token": f"{base}/oauth2/v2.0/token",
            "jwks": f"{base}/discovery/v2.0/keys"}


def redirect_uri(provider: str) -> str:
    return f"{config.settings.public_url}/api/auth/oidc/{provider}/callback"


# ------------------------------------------------------------------ the pending-sign-in cookie

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    key = hashlib.sha256(b"dq-oidc-cookie:" + config.settings.secret_key.encode()).digest()
    return _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())


def begin(provider: str) -> tuple[str, str]:
    """(the provider's sign-in address, the cookie value to set)."""
    cid = client(provider)[0]
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    params = {"response_type": "code", "client_id": cid, "redirect_uri": redirect_uri(provider),
              "scope": "openid email profile", "state": state, "nonce": nonce,
              "code_challenge": challenge, "code_challenge_method": "S256",
              "prompt": "select_account"}
    if provider == "microsoft":
        params["response_mode"] = "query"
    body = _b64(json.dumps({"p": provider, "s": state, "n": nonce, "v": verifier,
                            "e": int(time.time()) + COOKIE_SECONDS}).encode())
    return endpoints(provider)["authorize"] + "?" + urllib.parse.urlencode(params), \
        f"{body}.{_sign(body)}"


def read_cookie(value: str | None, provider: str) -> dict:
    if not value or "." not in value:
        raise OidcError("expired", "no pending sign-in cookie")
    body, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body)):
        raise OidcError("failed", "cookie signature does not match")
    try:
        data = json.loads(_unb64(body))
    except ValueError:
        raise OidcError("failed", "cookie unreadable") from None
    if data.get("p") != provider or int(data.get("e", 0)) < time.time():
        raise OidcError("expired", "pending sign-in is for another provider or too old")
    return {"state": data["s"], "nonce": data["n"], "verifier": data["v"]}


# ------------------------------------------------------------------ talking to the provider

def _request_json(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 (fixed https hosts)
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise OidcError("failed", f"provider answered {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise OidcError("failed", f"provider unreachable ({e.__class__.__name__})") from None


def exchange_code(provider: str, code: str, verifier: str) -> dict:
    """The provider's token response for an authorization code."""
    cid, secret = client(provider)
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(provider),
        "client_id": cid, "client_secret": secret, "code_verifier": verifier}).encode()
    req = urllib.request.Request(endpoints(provider)["token"], data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    return _request_json(req)


_jwks_cache: dict[str, tuple[float, dict]] = {}


def fetch_jwks(provider: str, refresh: bool = False) -> dict:
    """The provider's published signing keys, kept for an hour."""
    url = endpoints(provider)["jwks"]
    hit = _jwks_cache.get(url)
    if hit and not refresh and hit[0] > time.time():
        return hit[1]
    data = _request_json(urllib.request.Request(url, headers={"Accept": "application/json"}))
    _jwks_cache[url] = (time.time() + 3600, data)
    return data


def _signing_key(provider: str, kid: str | None):
    for refresh in (False, True):      # keys rotate: look again once before refusing
        for k in fetch_jwks(provider, refresh=refresh).get("keys", []):
            if k.get("kid") == kid and k.get("kty") == "RSA" and k.get("use", "sig") == "sig":
                try:
                    return PyJWK(k, algorithm="RS256").key
                except jwt.PyJWTError:
                    break
    raise OidcError("failed", "no published key matches the token")


# ------------------------------------------------------------------ checking the ID token

def _issuer_ok(provider: str, claims: dict) -> bool:
    iss = claims.get("iss")
    if provider == "google":
        return iss in GOOGLE_ISSUERS
    tid = str(claims.get("tid") or "").lower()
    if not tid or iss != f"https://login.microsoftonline.com/{tid}/v2.0":
        return False
    tenant = (config.settings.microsoft_tenant or "organizations").lower()
    if tenant == "organizations":
        return tid != MS_CONSUMER_TENANT
    if tenant == "consumers":
        return tid == MS_CONSUMER_TENANT
    return tenant == "common" or tid == tenant


def verify_id_token(provider: str, id_token: str | None, nonce: str) -> dict:
    if not id_token:
        raise OidcError("failed", "no ID token in the provider's answer")
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise OidcError("failed", "ID token unreadable") from None
    if header.get("alg") != "RS256":
        raise OidcError("failed", "ID token not signed with RS256")
    key = _signing_key(provider, header.get("kid"))
    cid = client(provider)[0]
    try:
        claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=cid, leeway=60,
                            options={"require": ["iss", "aud", "exp", "iat", "sub"]})
    except jwt.PyJWTError as e:
        raise OidcError("failed", f"ID token refused ({e.__class__.__name__})") from None
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != cid:
        raise OidcError("failed", "ID token issued to another party")
    if not _issuer_ok(provider, claims):
        raise OidcError("failed", "ID token from an unexpected issuer")
    if not nonce or not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise OidcError("failed", "ID token nonce does not match")
    return claims


def email_of(provider: str, claims: dict) -> str:
    if provider == "google":
        if claims.get("email_verified") not in (True, "true"):
            raise OidcError("unverified", "Google email not verified")
        email = claims.get("email")
    else:
        email = claims.get("email") or claims.get("preferred_username")
    email = str(email or "").strip().lower()
    if "@" not in email:
        raise OidcError("no-email", "no email address in the ID token")
    return email
