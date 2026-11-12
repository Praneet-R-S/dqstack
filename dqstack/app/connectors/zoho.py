"""Zoho Inventory: items and sales pulled in, nightly and on demand.

Connecting (OAuth 2.0, authorization code with offline access):

  An Admin registers a Zoho "server-based application" (or a "self client") in the
  Zoho API Console of their own Zoho account, and types its client id and secret
  into Connectors. DQ-Stack sends them to Zoho's consent page in the right data
  centre (Zoho runs separate ones: .in for India, .com, .eu, .com.au, .jp, .ca,
  .sa, .com.cn), Zoho sends them back to /api/connectors/zoho/callback with a code,
  and the code is exchanged for a refresh token. A self client has no redirect: the
  Admin pastes the code the console made. Only read scopes are asked for (`SCOPES`).

  The client secret and the tokens are stored encrypted (secrets.py) and never leave
  the server. An access token lasts an hour; a fresh one is fetched with the refresh
  token when it runs out or Zoho answers 401.

Syncing:

  * Items, newest change first (sort by last_modified_time), until one older than the
    last sync's cursor (less two minutes, so nothing on the boundary is missed). Each
    becomes a row of a products file, run through the Imports pipeline: a repeat
    changes nothing, and Undo works as for a spreadsheet.
  * Sales: invoices, or sales orders if the company chose them, the same way. Each
    changed document is fetched once for its lines and kept, reduced, in
    connector_docs. The days it touches (its old day and its new one) are rebuilt
    from every kept document of that day and imported as a sales file, which replaces
    each store-day it holds; so an edited or voided invoice corrects its day instead
    of adding to it. Drafts and void documents count for nothing.
  * Warehouses map to stores (the Connectors screen). Lines with no warehouse go to
    the default store, if one is chosen; lines from a warehouse not mapped are left
    out and counted.
  * Zoho allows 100 requests a minute per organisation. The client spaces requests to
    `rate_per_minute` (90 by default), and on 429 waits as told (Retry-After) or
    doubling from 2 seconds, up to 6 times.
  * A sync is one transaction: a failure leaves nothing half-done and the cursor
    where it was, so the next sync simply tries again. Failures raise an alert for
    Owners and Admins, with the reason in plain words.

Deleted (not voided) invoices do not appear in Zoho's lists, so a sync cannot see
them; "Sync everything again" rebuilds the period from scratch and drops them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets as pysecrets
import time as pytime
import urllib.parse
from collections import deque
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..db import utcnow
from ..imports import engine as E
from ..models import Alert, Company, Connector, ConnectorDoc, ConnectorRun, Store
from ..security import _aware
from . import secrets as vault
from .importing import counts, make_job, system_ctx
from .tally import PRODUCT_HEADERS, SALES_HEADERS, get_connector, our_unit

log = logging.getLogger("dqstack.connectors.zoho")

# data centre -> (accounts host, API host, label)
DATA_CENTRES: dict[str, tuple[str, str, str]] = {
    "in": ("accounts.zoho.in", "www.zohoapis.in", "India (zoho.in)"),
    "com": ("accounts.zoho.com", "www.zohoapis.com", "United States (zoho.com)"),
    "eu": ("accounts.zoho.eu", "www.zohoapis.eu", "Europe (zoho.eu)"),
    "com.au": ("accounts.zoho.com.au", "www.zohoapis.com.au", "Australia (zoho.com.au)"),
    "jp": ("accounts.zoho.jp", "www.zohoapis.jp", "Japan (zoho.jp)"),
    "ca": ("accounts.zohocloud.ca", "www.zohoapis.ca", "Canada (zohocloud.ca)"),
    "sa": ("accounts.zoho.sa", "www.zohoapis.sa", "Saudi Arabia (zoho.sa)"),
    "com.cn": ("accounts.zoho.com.cn", "www.zohoapis.com.cn", "China (zoho.com.cn)"),
}
ACCOUNT_HOSTS = {v[0] for v in DATA_CENTRES.values()}
API_HOSTS = {v[1] for v in DATA_CENTRES.values()}
# Read-only, and only what a sync reads: items, invoices, sales orders, and the
# settings scope for the organisation and its warehouses.
SCOPES = ("ZohoInventory.items.READ", "ZohoInventory.invoices.READ",
          "ZohoInventory.salesorders.READ", "ZohoInventory.settings.READ")
SOURCES = {"invoices": ("invoice", "invoice_id", "invoice"),
           "salesorders": ("salesorder", "salesorder_id", "salesorder")}

DEFAULTS: dict = {
    "data_centre": "in",
    "organization_id": "",
    "organization_name": "",
    "organizations": [],
    "sales_source": "invoices",        # invoices | salesorders
    "sales_into": "daily_sales",       # daily_sales (moves stock) | sales_history (forecast only)
    "warehouse_map": {},               # Zoho warehouse id -> DQ-Stack store id
    "default_store_id": "",            # for lines with no warehouse
    "warehouses": [],                  # [{"id", "name"}] as last read from Zoho
    "start_date": "",                  # first sync reads sales from here (default 90 days)
    "sync_time": "00:30",              # nightly, company time
    "rate_per_minute": 90,
}
OVERLAP = timedelta(minutes=2)
MAX_429 = 6
MAX_PAGES = 1000
PER_PAGE = 200

# Swapped by the tests for a mocked Zoho and a clock that does not really wait.
TRANSPORT: httpx.BaseTransport | None = None
SLEEP = pytime.sleep
CLOCK = pytime.monotonic


class ZohoError(Exception):
    """`message` is for the person reading the Connectors screen or the alert;
    `detail` is for support. `reconnect` means the stored access is no longer good."""

    def __init__(self, message: str, detail: str = "", reconnect: bool = False):
        super().__init__(message)
        self.message, self.detail, self.reconnect = message, detail, reconnect


def settings_of(conn: Connector | None) -> dict:
    s = dict(DEFAULTS)
    s.update((conn.settings or {}) if conn else {})
    return s


def hosts(s: dict, creds: dict | None = None) -> tuple[str, str]:
    acc, api, _ = DATA_CENTRES.get(s.get("data_centre") or "in", DATA_CENTRES["in"])
    if creds:
        acc = creds.get("accounts_host") if creds.get("accounts_host") in ACCOUNT_HOSTS else acc
        api = creds.get("api_host") if creds.get("api_host") in API_HOSTS else api
    return acc, api


def redirect_uri() -> str:
    return f"{config.settings.public_url}/api/connectors/zoho/callback"


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    return urllib.parse.urlparse(url if "//" in url else "https://" + url).hostname


def _http() -> httpx.Client:
    return httpx.Client(transport=TRANSPORT, timeout=httpx.Timeout(30.0),
                        headers={"User-Agent": "DQ-Stack connector"})


def _json(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        raise ZohoError("Zoho sent an answer DQ-Stack could not read. Try again later.",
                        f"HTTP {r.status_code}: {r.text[:300]}") from None
    return body if isinstance(body, dict) else {"data": body}


# =================================================================== OAuth

def authorize_url(s: dict, client_id: str, state: str) -> str:
    acc, _ = hosts(s)
    q = urllib.parse.urlencode({"response_type": "code", "client_id": client_id,
                                "scope": ",".join(SCOPES), "redirect_uri": redirect_uri(),
                                "access_type": "offline", "prompt": "consent", "state": state})
    return f"https://{acc}/oauth/v2/auth?{q}"


def new_state() -> tuple[str, str]:
    raw = pysecrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def exchange_code(s: dict, creds: dict, code: str, accounts_server: str | None = None,
                  with_redirect: bool = True) -> dict:
    """Swap a grant code for tokens. Zoho names the data centre the person signed in
    to in `accounts-server`; it is used only if it is one of Zoho's own hosts, so a
    forged callback cannot send the client secret anywhere else."""
    acc, _ = hosts(s)
    named = _host_of(accounts_server)
    if named in ACCOUNT_HOSTS:
        acc = named
    form = {"grant_type": "authorization_code", "client_id": creds["client_id"],
            "client_secret": creds["client_secret"], "code": code.strip()}
    if with_redirect:
        form["redirect_uri"] = redirect_uri()
    with _http() as http:
        try:
            r = http.post(f"https://{acc}/oauth/v2/token", data=form)
        except httpx.HTTPError as e:
            raise ZohoError("Zoho's sign-in service could not be reached. Try again in a "
                            "few minutes.", str(e)) from None
    body = _json(r)
    if not body.get("refresh_token"):
        err = body.get("error") or f"HTTP {r.status_code}"
        msg = {"invalid_code": "That code was not accepted: it may have expired (codes "
                               "last a few minutes) or been used already. Start again.",
               "invalid_client": "Zoho did not recognise the client id. Check it in the Zoho "
                                 "API Console and that the data centre is right.",
               "invalid_client_secret": "Zoho did not accept the client secret. Copy it "
                                        "again from the Zoho API Console.",
               "invalid_redirect_uri": f"Zoho's app does not list {redirect_uri()} as its "
                                       "redirect address. Add it in the Zoho API Console."
               }.get(err, f"Zoho did not give DQ-Stack access ({err}).")
        raise ZohoError(msg, json.dumps(body)[:500])
    api = _host_of(body.get("api_domain"))
    out = dict(creds)
    out.update(refresh_token=body["refresh_token"], access_token=body.get("access_token"),
               access_expires=pytime.time() + int(body.get("expires_in") or 3600) - 120,
               accounts_host=acc, api_host=api if api in API_HOSTS else hosts(s)[1])
    return out


def revoke(s: dict, creds: dict | None) -> None:
    """Best effort: tell Zoho to forget the refresh token. Disconnecting does not wait
    on Zoho; the token is wiped here either way."""
    if not creds or not creds.get("refresh_token"):
        return
    acc, _ = hosts(s, creds)
    try:
        with _http() as http:
            http.post(f"https://{acc}/oauth/v2/token/revoke",
                      data={"token": creds["refresh_token"]})
    except httpx.HTTPError:
        log.warning("Zoho token revoke did not reach Zoho")


# =================================================================== the API client

class Api:
    """GET requests to Zoho Inventory with the rate limit, retries and token refresh."""

    def __init__(self, creds: dict, s: dict):
        self.creds = dict(creds)
        self.s = s
        self.org = s.get("organization_id") or ""
        self.http = _http()
        self.limit = max(1, min(100, int(s.get("rate_per_minute") or 90)))
        self.calls: deque[float] = deque()
        self.requests = 0
        self.slowed = 0
        self.refreshed = 0
        self.changed = False

    def close(self) -> None:
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _throttle(self) -> None:
        now = CLOCK()
        while self.calls and now - self.calls[0] >= 60:
            self.calls.popleft()
        if len(self.calls) >= self.limit:
            SLEEP(max(0.0, 60 - (now - self.calls[0])))
            now = CLOCK()
            while self.calls and now - self.calls[0] >= 60:
                self.calls.popleft()
        self.calls.append(CLOCK())

    def refresh(self) -> None:
        acc, _ = hosts(self.s, self.creds)
        form = {"grant_type": "refresh_token", "refresh_token": self.creds.get("refresh_token"),
                "client_id": self.creds.get("client_id"),
                "client_secret": self.creds.get("client_secret")}
        try:
            r = self.http.post(f"https://{acc}/oauth/v2/token", data=form)
        except httpx.HTTPError as e:
            raise ZohoError("Zoho's sign-in service could not be reached. The sync will try "
                            "again.", str(e)) from None
        body = _json(r)
        if not body.get("access_token"):
            err = body.get("error") or f"HTTP {r.status_code}"
            if err in ("invalid_code", "invalid_client", "invalid_client_secret",
                       "unauthorized_client", "access_denied") or r.status_code in (400, 401):
                raise ZohoError("Zoho no longer accepts DQ-Stack's access to your Zoho "
                                "Inventory (the access was removed, or the app's secret "
                                "changed). Connect again on the Connectors screen.",
                                json.dumps(body)[:500], reconnect=True)
            raise ZohoError(f"Zoho did not renew DQ-Stack's access ({err}). The sync will "
                            "try again.", json.dumps(body)[:500])
        self.creds["access_token"] = body["access_token"]
        self.creds["access_expires"] = pytime.time() + int(body.get("expires_in") or 3600) - 120
        api = _host_of(body.get("api_domain"))
        if api in API_HOSTS:
            self.creds["api_host"] = api
        self.refreshed += 1
        self.changed = True

    def get(self, path: str, params: dict | None = None, org: bool = True) -> dict:
        if not self.creds.get("access_token") or \
                float(self.creds.get("access_expires") or 0) <= pytime.time():
            self.refresh()
        _, api = hosts(self.s, self.creds)
        url = f"https://{api}/inventory/v1/{path}"
        q = dict(params or {})
        if org:
            q["organization_id"] = self.org
        renewed, tries_429, tries_err = False, 0, 0
        while True:
            self._throttle()
            self.requests += 1
            try:
                r = self.http.get(url, params=q, headers={
                    "Authorization": f"Zoho-oauthtoken {self.creds['access_token']}"})
            except httpx.HTTPError as e:
                tries_err += 1
                if tries_err > 3:
                    raise ZohoError("Zoho Inventory could not be reached. The sync will try "
                                    "again.", f"{path}: {e}") from None
                SLEEP(2 ** tries_err)
                continue
            if r.status_code == 429:
                tries_429 += 1
                self.slowed += 1
                if tries_429 > MAX_429:
                    raise ZohoError("Zoho kept asking DQ-Stack to slow down (more than 100 "
                                    "requests a minute for your organisation, or the day's "
                                    "allowance is used up). The sync will try again later.",
                                    f"{path}: 429 x{tries_429}")
                SLEEP(_retry_after(r) or min(60, 2 ** tries_429))
                continue
            if r.status_code == 401 and not renewed:
                self.refresh()
                renewed = True
                continue
            if r.status_code >= 500:
                tries_err += 1
                if tries_err > 3:
                    raise ZohoError("Zoho Inventory is having trouble (it answered with an "
                                    "error several times). The sync will try again.",
                                    f"{path}: HTTP {r.status_code} {r.text[:200]}")
                SLEEP(2 ** tries_err)
                continue
            body = _json(r)
            code = body.get("code", 0)
            if r.status_code == 401:
                raise ZohoError("Zoho refused DQ-Stack's access. Connect again on the "
                                "Connectors screen.", f"{path}: {body}"[:500], reconnect=True)
            if r.status_code >= 400 or code not in (0, "0", None):
                msg = str(body.get("message") or f"HTTP {r.status_code}")
                if code == 57 or r.status_code == 403:
                    raise ZohoError(f"Zoho says DQ-Stack may not read this ({msg}). Check the "
                                    "organisation chosen, and that the connection was made "
                                    "with the scopes listed on the Connectors screen.",
                                    f"{path}: {body}"[:500])
                raise ZohoError(f"Zoho Inventory answered: {msg}", f"{path}: {body}"[:500])
            return body

    def pages(self, path: str, key: str, params: dict, stop=None):
        """Every record of a list, page by page, until `stop(record)` is true."""
        page = 1
        while True:
            body = self.get(path, {**params, "page": page, "per_page": PER_PAGE})
            for row in body.get(key) or []:
                if stop is not None and stop(row):
                    return
                yield row
            if not (body.get("page_context") or {}).get("has_more_page"):
                return
            page += 1
            if page > MAX_PAGES:
                raise ZohoError("Zoho returned more pages than a sync reads at once. Choose a "
                                "later start date.")


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("Retry-After")
    try:
        return min(300.0, max(0.0, float(v))) if v else None
    except ValueError:
        return None


# =================================================================== reading Zoho's records

def zoho_time(raw: str | None) -> datetime | None:
    """'2026-11-10T12:30:15+0530' -> an aware datetime in UTC."""
    if not raw:
        return None
    for parse in (datetime.fromisoformat,
                  lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")):
        try:
            d = parse(raw)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _dec(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _fp(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def _tax_pct(rec: dict) -> Decimal | None:
    t = _dec(rec.get("tax_percentage"))
    if t is not None:
        return t
    for pref in rec.get("item_tax_preferences") or []:
        if str(pref.get("tax_specification", "")).lower() == "intra":
            return _dec(pref.get("tax_percentage"))
    return None


def item_data(it: dict) -> dict:
    return {"sku": (it.get("sku") or "").strip(), "name": (it.get("name") or "").strip(),
            "rate": str(_dec(it.get("rate")) or ""),
            "purchase_rate": str(_dec(it.get("purchase_rate")) or ""),
            "unit": (it.get("unit") or "").strip(), "hsn": (it.get("hsn_or_sac") or "").strip(),
            "tax": str(_tax_pct(it) if _tax_pct(it) is not None else ""),
            "status": (it.get("status") or "active").lower()}


def product_row(d: dict) -> list:
    rate = _dec(d["rate"])
    tax = _dec(d["tax"])
    if rate is not None and tax is not None and not d.get("inclusive"):
        rate = (rate * (100 + tax) / 100).quantize(Decimal("0.01"))
    hsn = "".join(ch for ch in d["hsn"] if ch.isdigit())
    return [d["sku"], d["name"], "", our_unit(d["unit"]), "",
            "" if rate is None else format(rate, "f"), d["purchase_rate"],
            "" if tax is None else format(tax.normalize(), "f"),
            hsn if 4 <= len(hsn) <= 8 else "",
            "discontinued" if d["status"] == "inactive" else "active"]


def doc_lines(doc: dict, items: dict[str, str]) -> list[dict]:
    """A document's lines reduced to what a day's sales need: warehouse, SKU, quantity
    and what the customer paid including GST, in paise."""
    inclusive = bool(doc.get("is_inclusive_tax"))
    head_w = str(doc.get("location_id") or doc.get("warehouse_id") or "")
    out = []
    for ln in doc.get("line_items") or []:
        qty = _dec(ln.get("quantity")) or Decimal(0)
        total = _dec(ln.get("item_total"))
        if total is None:
            total = (_dec(ln.get("rate")) or Decimal(0)) * qty
        tax = _tax_pct(ln) or Decimal(0)
        paid = total if inclusive else total * (100 + tax) / 100
        sku = (ln.get("sku") or "").strip() or items.get(str(ln.get("item_id") or ""), "")
        out.append({"w": str(ln.get("warehouse_id") or ln.get("location_id") or head_w),
                    "sku": sku, "item": str(ln.get("item_id") or ""),
                    "q": format(qty.normalize(), "f"),
                    "r": int((paid * 100).quantize(Decimal("1")))})
    return out


# =================================================================== sync

def _alert(db: Session, conn: Connector, message: str) -> None:
    from ..sales import local_today, tz_of
    day = local_today(tz_of(db, conn.company_id))
    key = f"connector_failed:{conn.id}:{day.isoformat()}"
    a = db.scalar(select(Alert).where(Alert.company_id == conn.company_id,
                                      Alert.dedupe_key == key))
    if a is not None:
        a.resolved_at = None
        return
    db.add(Alert(company_id=conn.company_id, kind="connector_failed", severity="risk",
                 message=f"Zoho Inventory sync: {message}"[:500], dedupe_key=key,
                 link="#/connectors"))


def _resolve_alerts(db: Session, conn: Connector) -> None:
    for a in db.scalars(select(Alert).where(Alert.company_id == conn.company_id,
                                            Alert.kind == "connector_failed",
                                            Alert.resolved_at.is_(None),
                                            Alert.dedupe_key.like(f"connector_failed:{conn.id}:%"))):
        a.resolved_at = utcnow()


def _stamp() -> str:
    from ..formats import IST
    return utcnow().astimezone(IST).strftime("%d %b %Y %H:%M")


def _commit_job(db: Session, c, job) -> dict:
    if job.status == "checked":
        E.commit(db, c, job)
    return counts(job)


def _pull(db: Session, conn: Connector, s: dict, api: Api, full: bool, run: ConnectorRun) -> dict:
    c = system_ctx(conn.company_id, "Zoho Inventory", "connector:zoho")
    cursor = {} if full else dict(conn.cursor or {})
    summary: dict = {"full": full}
    # ------------------------------------------------------------ items
    since = zoho_time(cursor.get("items"))
    newest = since
    cached = {d.external_id: d for d in db.scalars(select(ConnectorDoc).where(
        ConnectorDoc.connector_id == conn.id, ConnectorDoc.doc_kind == "item"))}
    rows, no_sku, read = [], [], 0

    def older(rec) -> bool:
        t = zoho_time(rec.get("last_modified_time"))
        return since is not None and t is not None and t < since - OVERLAP

    for it in api.pages("items", "items", {"sort_column": "last_modified_time",
                                           "sort_order": "D"}, stop=older):
        read += 1
        iid = str(it.get("item_id") or "")
        if not iid:
            continue
        mod = zoho_time(it.get("last_modified_time"))
        if mod and (newest is None or mod > newest):
            newest = mod
        data = item_data(it)
        fp = _fp(data)
        doc = cached.get(iid)
        if doc is not None and doc.fingerprint == fp and not full:
            continue
        if doc is None:
            doc = ConnectorDoc(company_id=conn.company_id, connector_id=conn.id,
                               doc_kind="item", external_id=iid[:64])
            db.add(doc)
            cached[iid] = doc
        doc.fingerprint, doc.data, doc.modified_at = fp, data, mod
        if not data["sku"]:
            no_sku.append(data["name"] or iid)
            continue
        rows.append(product_row(data))
    summary["items"] = {"read": read, "changed": len(rows), "no_sku": len(no_sku),
                        "no_sku_names": no_sku[:20]}
    if rows:
        job = make_job(db, c, "products", f"Zoho Inventory items, {_stamp()}",
                       PRODUCT_HEADERS + ["Status"], rows)
        summary["items"]["import"] = _commit_job(db, c, job)
        run.import_job_ids = [*run.import_job_ids, job.id]
    if newest:
        cursor["items"] = newest.isoformat()
    db.flush()

    # ------------------------------------------------------------ sales documents
    source = s["sales_source"] if s["sales_source"] in SOURCES else "invoices"
    doc_kind, id_key, one = SOURCES[source]
    since = zoho_time(cursor.get(source))
    newest = since
    from ..sales import local_today, tz_of
    tz = tz_of(db, conn.company_id)
    try:
        start = date.fromisoformat(s["start_date"]) if s.get("start_date") else None
    except ValueError:
        start = None
    if start is None:
        start = local_today(tz) - timedelta(days=90)
    params = {"sort_column": "last_modified_time", "sort_order": "D"}
    if since is None:
        params["date_start"] = start.isoformat()
    item_skus = {k: (d.data or {}).get("sku", "") for k, d in cached.items()}
    kept = {d.external_id: d for d in db.scalars(select(ConnectorDoc).where(
        ConnectorDoc.connector_id == conn.id, ConnectorDoc.doc_kind == doc_kind))}
    affected: set[date] = set()
    old_lines: dict[date, list[dict]] = {}
    seen: set[str] = set()
    read = fetched = dropped = 0

    def remember_old(doc: ConnectorDoc) -> None:
        if doc.day is not None:
            affected.add(doc.day)
            old_lines.setdefault(doc.day, []).extend((doc.data or {}).get("lines") or [])

    def older2(rec) -> bool:
        t = zoho_time(rec.get("last_modified_time"))
        return since is not None and t is not None and t < since - OVERLAP

    for rec in api.pages(source, source, params, stop=older2):
        read += 1
        eid = str(rec.get(id_key) or "")
        try:
            day = date.fromisoformat(str(rec.get("date") or "")[:10])
        except ValueError:
            continue
        if not eid or day < start:
            continue
        seen.add(eid)
        mod = zoho_time(rec.get("last_modified_time"))
        if mod and (newest is None or mod > newest):
            newest = mod
        status = str(rec.get("status") or "").lower()
        old = kept.get(eid)
        if status in ("draft", "void"):
            if old is not None:
                remember_old(old)
                db.delete(old)
                kept.pop(eid)
                dropped += 1
            continue
        if old is not None and not full and old.modified_at is not None and mod is not None \
                and _aware(old.modified_at) == mod:
            continue                                   # unchanged since we last read it
        detail = api.get(f"{source}/{eid}").get(one) or {}
        fetched += 1
        data = {"number": str(detail.get(f"{one}_number") or rec.get(f"{one}_number") or ""),
                "status": status, "lines": doc_lines(detail, item_skus)}
        fp = _fp({"day": day.isoformat(), **data})
        if old is not None and old.fingerprint == fp and not full:
            old.modified_at = mod
            continue
        if old is not None:
            remember_old(old)
        else:
            old = ConnectorDoc(company_id=conn.company_id, connector_id=conn.id,
                               doc_kind=doc_kind, external_id=eid[:64])
            db.add(old)
            kept[eid] = old
        old.day, old.fingerprint, old.data, old.modified_at = day, fp, data, mod
        affected.add(day)
    if full:
        # Documents kept from before that Zoho no longer lists were deleted there.
        for eid, doc in list(kept.items()):
            if eid not in seen and doc.day is not None and doc.day >= start:
                remember_old(doc)
                db.delete(doc)
                kept.pop(eid)
                dropped += 1
        affected |= {d.day for d in kept.values() if d.day is not None and d.day >= start}
    if newest:
        cursor[source] = newest.isoformat()
    db.flush()
    summary["sales"] = {"source": source, "read": read, "fetched": fetched, "removed": dropped,
                        "days": len(affected)}
    if affected:
        rows, unmapped, no_sku_lines = _day_rows(db, conn, s, doc_kind, affected, old_lines)
        summary["sales"].update(unmapped_lines=unmapped, lines_without_sku=no_sku_lines)
        if rows:
            kind = s["sales_into"] if s["sales_into"] in ("daily_sales", "sales_history") \
                else "daily_sales"
            job = make_job(db, c, kind, f"Zoho Inventory {source}, {_stamp()}",
                           SALES_HEADERS, rows)
            summary["sales"]["import"] = _commit_job(db, c, job)
            run.import_job_ids = [*run.import_job_ids, job.id]
    return {"cursor": cursor, "summary": summary}


def _day_rows(db: Session, conn: Connector, s: dict, doc_kind: str, days: set[date],
              old_lines: dict[date, list[dict]]) -> tuple[list[list], int, int]:
    """Every kept document of the affected days, added up by store, day and SKU. Lines
    that used to be on those days get a zero row, so a store-day whose last invoice was
    voided is still in the file, and is replaced (emptied) rather than left alone."""
    stores = {st.id: st for st in db.scalars(select(Store).where(
        Store.company_id == conn.company_id))}
    wmap = {str(k): v for k, v in (s.get("warehouse_map") or {}).items() if v in stores}
    default = s.get("default_store_id") if s.get("default_store_id") in stores else None

    def store_of(w: str) -> Store | None:
        sid = wmap.get(w) if w else default
        return stores.get(sid) if sid else None

    totals: dict[tuple, list] = {}
    unmapped = no_sku = 0
    docs = db.scalars(select(ConnectorDoc).where(
        ConnectorDoc.connector_id == conn.id, ConnectorDoc.doc_kind == doc_kind,
        ConnectorDoc.day.in_(sorted(days))))
    for d in docs:
        for ln in (d.data or {}).get("lines") or []:
            st = store_of(ln.get("w") or "")
            if st is None:
                unmapped += 1
                continue
            if not ln.get("sku"):
                no_sku += 1
                continue
            t = totals.setdefault((d.day, st.code, ln["sku"]), [Decimal(0), 0])
            t[0] += _dec(ln.get("q")) or Decimal(0)
            t[1] += int(ln.get("r") or 0)
    for day, lines in old_lines.items():
        for ln in lines:
            st = store_of(ln.get("w") or "")
            if st is not None and ln.get("sku"):
                totals.setdefault((day, st.code, ln["sku"]), [Decimal(0), 0])
    rows = [[day.isoformat(), code, sku, format(q.normalize(), "f"), f"{Decimal(r) / 100:.2f}"]
            for (day, code, sku), (q, r) in sorted(totals.items())]
    return rows, unmapped, no_sku


def _plain(summary: dict) -> str:
    it, sa = summary.get("items", {}), summary.get("sales", {})
    parts = []
    imp = it.get("import")
    parts.append(f"{it.get('read', 0)} items read" + (
        f", {imp['new']} new and {imp['updated']} changed" if imp else ", none changed"))
    word = "invoices" if sa.get("source") == "invoices" else "sales orders"
    simp = sa.get("import")
    parts.append(f"{sa.get('fetched', 0)} {word} new or changed" + (
        f"; sales for {sa.get('days', 0)} day{'s' if sa.get('days') != 1 else ''} updated"
        if simp else ""))
    notes = []
    if it.get("no_sku"):
        notes.append(f"{it['no_sku']} Zoho item{'s have' if it['no_sku'] != 1 else ' has'} no "
                     "SKU and were left out: add one in Zoho")
    if sa.get("unmapped_lines"):
        notes.append(f"{sa['unmapped_lines']} sales lines are from a warehouse not matched to "
                     "a store, and were left out")
    if sa.get("lines_without_sku"):
        notes.append(f"{sa['lines_without_sku']} sales lines have no SKU")
    refused = (imp or {}).get("failed", 0) + (simp or {}).get("failed", 0)
    if refused:
        notes.append(f"{refused} rows were refused: open the import to see why")
    return "; ".join(parts) + "." + (" " + ". ".join(n[0].upper() + n[1:] for n in notes) + "."
                                     if notes else "")


def sync(db: Session, conn: Connector, trigger: str = "schedule", person_id: str | None = None,
         full: bool | None = None) -> ConnectorRun:
    """One sync, recorded as a ConnectorRun. Never raises for a Zoho problem: the run
    says what went wrong, and an alert is raised."""
    s = settings_of(conn)
    if full is None:
        full = bool(s.get("full_next"))
    run = ConnectorRun(company_id=conn.company_id, connector_id=conn.id, kind="zoho",
                       trigger=trigger, status="running", person_id=person_id, summary={},
                       import_job_ids=[])
    db.add(run)
    settings = dict(conn.settings or {})
    settings.pop("sync_requested", None)
    conn.settings = settings
    db.commit()
    run_id, conn_id = run.id, conn.id
    try:
        creds = vault.unseal(conn.credentials)
        if conn.status not in ("connected",) or not creds or not creds.get("refresh_token"):
            raise ZohoError("Zoho Inventory is not connected, or its stored access can no longer "
                            "be read. Connect again on the Connectors screen.", reconnect=True)
        if not s.get("organization_id"):
            raise ZohoError("Choose which Zoho organisation to read on the Connectors screen.")
        with Api(creds, s) as api:
            out = _pull(db, conn, s, api, bool(full), run)
            summary = {**out["summary"], "requests": api.requests, "slowed_down": api.slowed,
                       "token_renewed": api.refreshed}
            if api.changed:
                conn.credentials = vault.seal(api.creds)
        refused = sum((summary.get(k, {}).get("import") or {}).get("failed", 0)
                      for k in ("items", "sales"))
        left_out = sum(summary.get(a, {}).get(b) or 0 for a, b in (
            ("items", "no_sku"), ("sales", "unmapped_lines"), ("sales", "lines_without_sku")))
        run.summary = summary
        run.status = "warning" if refused or left_out else "succeeded"
        run.message = _plain(summary)
        run.finished_at = utcnow()
        conn.cursor = out["cursor"]
        conn.last_sync_at = run.finished_at
        conn.last_error = None
        settings = dict(conn.settings or {})
        settings.pop("full_next", None)
        conn.settings = settings
        conn.updated_at = run.finished_at
        if refused:
            _alert(db, conn, run.message)
        else:
            _resolve_alerts(db, conn)
        db.commit()
        return run
    except ZohoError as e:
        db.rollback()
        return _failed(db, conn_id, run_id, e.message, e.detail, e.reconnect)
    except Exception as e:  # noqa: BLE001 -- a bug must not stop the worker; it is logged
        db.rollback()
        log.exception("Zoho sync for connector %s failed", conn_id)
        return _failed(db, conn_id, run_id, "Something went wrong while reading Zoho "
                       "Inventory. Nothing was changed; the details are in the server's log.",
                       f"{type(e).__name__}: {e}"[:500], False)


def _failed(db: Session, conn_id: str, run_id: str, message: str, detail: str,
            reconnect: bool) -> ConnectorRun:
    conn = db.get(Connector, conn_id)
    run = db.get(ConnectorRun, run_id)
    run.status, run.message, run.error, run.finished_at = "failed", message, detail or None, utcnow()
    conn.last_error = message
    if reconnect and conn.status == "connected":
        conn.status = "needs_reconnect"
    _alert(db, conn, message)
    db.commit()
    return run


# =================================================================== the schedule

def due(db: Session, conn: Connector, now: datetime) -> str | None:
    if conn.status != "connected":
        return None
    s = settings_of(conn)
    if s.get("sync_requested"):
        return "manual"
    from ..sales import tz_of
    local = now.astimezone(tz_of(db, conn.company_id))
    try:
        hh, mm = (int(x) for x in str(s.get("sync_time") or "00:30").split(":"))
    except ValueError:
        hh, mm = 0, 30
    if local.time() < time(hh, mm) or s.get("last_scheduled_day") == local.date().isoformat():
        return None
    return "schedule"


def run_due(now: datetime | None = None) -> list[tuple[str, str]]:
    """Called by the worker every pass: each connected Zoho connector whose night has
    come, or whose Admin pressed "Sync now"."""
    from ..db import SessionLocal
    now = now or utcnow()
    done = []
    with SessionLocal() as db:
        ids = list(db.scalars(select(Connector.id).join(Company, Company.id == Connector.company_id)
                              .where(Connector.kind == "zoho", Connector.status == "connected",
                                     Company.status == "active")))
    for cid in ids:
        with SessionLocal() as db:
            conn = db.get(Connector, cid)
            trigger = due(db, conn, now)
            if trigger is None:
                continue
            if trigger == "schedule":
                from ..sales import tz_of
                st = dict(conn.settings or {})
                st["last_scheduled_day"] = now.astimezone(tz_of(db, conn.company_id)).date().isoformat()
                conn.settings = st
                db.commit()
            person = (conn.settings or {}).get("sync_requested_by") if trigger == "manual" else None
            run = sync(db, conn, trigger, person_id=person)
            done.append((conn.company_id, run.status))
    return done


def warehouses(api: Api) -> list[dict]:
    """The organisation's warehouses, or its locations (Zoho's newer name for them)."""
    try:
        body = api.get("settings/warehouses")
        rows = body.get("warehouses") or []
        return [{"id": str(w.get("warehouse_id")), "name": w.get("warehouse_name") or "",
                 "active": str(w.get("status") or "active").lower() == "active"} for w in rows]
    except ZohoError as first:
        try:
            body = api.get("locations")
        except ZohoError:
            raise first from None
        return [{"id": str(w.get("location_id")), "name": w.get("location_name") or "",
                 "active": str(w.get("status") or "active").lower() == "active"}
                for w in body.get("locations") or []]


def organizations(api: Api) -> list[dict]:
    body = api.get("organizations", org=False)
    return [{"id": str(o.get("organization_id")), "name": o.get("name") or ""}
            for o in body.get("organizations") or []]


def connector(db: Session, company_id: str) -> Connector:
    return get_connector(db, company_id, "zoho")


def forget(db: Session, conn: Connector) -> None:
    """Disconnect: the tokens go, and with them the documents read with them."""
    db.execute(delete(ConnectorDoc).where(ConnectorDoc.connector_id == conn.id))
    conn.credentials = None
    conn.status = "not_connected"
    conn.cursor = {}
    st = dict(conn.settings or {})
    for k in ("oauth_state", "oauth_state_expires", "sync_requested", "full_next",
              "organizations"):
        st.pop(k, None)
    conn.settings = st
    conn.updated_at = utcnow()
