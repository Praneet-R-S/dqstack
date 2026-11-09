"""WhatsApp Business API and SMS: purchase orders to suppliers, alerts to staff and
receipts to customers.

No test talks to Meta or an SMS gateway: `urllib.request.urlopen` is replaced by a
fake that records each request and answers the way the Graph API does, so the tests
check what the server would send (address, token, body) and what it does when the
answer is a refusal.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest
from sqlalchemy import select

from dqstack.app import db as dbmod
from dqstack.app import alerts as A
from dqstack.app import ledger, sms, whatsapp
from dqstack.app.models import Alert, Company, Person, Product, Supplier

from .conftest import invite_and_join

WA_ENV = ("DQ_WHATSAPP_TOKEN", "DQ_WHATSAPP_PHONE_NUMBER_ID", "DQ_WHATSAPP_API_VERSION",
          "DQ_WHATSAPP_TEMPLATE_PO", "DQ_WHATSAPP_TEMPLATE_ALERT", "DQ_WHATSAPP_TEMPLATE_RECEIPT",
          "DQ_WHATSAPP_LANGUAGE", "DQ_SMS_PROVIDER", "DQ_SMS_WEBHOOK_URL", "DQ_SMS_WEBHOOK_TOKEN")


class _Resp:
    def __init__(self, body: dict, status: int = 200):
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """Stands in for urlopen. `fail` makes the next /messages call fail like Meta does."""

    def __init__(self):
        self.requests: list[urllib.request.Request] = []
        self.fail: tuple[int, dict] | None = None

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        url = req.full_url
        if url.endswith("/media"):
            return _Resp({"id": "media-77"})
        if url.endswith("/messages"):
            if self.fail:
                code, err = self.fail
                raise urllib.error.HTTPError(url, code, "Bad Request", {},
                                             io.BytesIO(json.dumps({"error": err}).encode()))
            return _Resp({"messaging_product": "whatsapp",
                          "messages": [{"id": f"wamid.{len(self.requests)}"}]})
        if url.startswith("https://sms.example/"):
            return _Resp({"ok": True})
        raise AssertionError(f"unexpected request to {url}")

    def json(self, i=-1) -> dict:
        return json.loads(self.requests[i].data)


@pytest.fixture
def net(monkeypatch):
    for k in WA_ENV:
        monkeypatch.delenv(k, raising=False)
    whatsapp.outbox.clear()
    sms.outbox.clear()
    fake = FakeNet()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def _configure(monkeypatch, **templates):
    monkeypatch.setenv("DQ_WHATSAPP_TOKEN", "test-token-abc")
    monkeypatch.setenv("DQ_WHATSAPP_PHONE_NUMBER_ID", "1098765432")
    for kind, name in templates.items():
        monkeypatch.setenv(f"DQ_WHATSAPP_TEMPLATE_{kind.upper()}", name)


GRAPH = "https://graph.facebook.com/v21.0/1098765432"


# ================================================================= purchase orders

def _approved_order(make_client, phone="98220 12345"):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "Andheri"}).json()
    owner.import_file("products", "SKU,Name,Unit,MRP,Selling price,Cost,Case pack\n"
                                  "DAL,Toor dal 1 kg,each,60,55,35,12\n")
    owner.import_file("suppliers", "Supplier,Email,Phone,Lead time days\n"
                                   f"Gupta Traders,orders@gupta.example,{phone},2\n")
    with dbmod.SessionLocal() as db:
        pid = db.scalar(select(Product)).id
        sup = db.scalar(select(Supplier)).id
    po = owner.post("/api/orders", {"store_id": s1["id"], "supplier_id": sup, "lines": [
        {"product_id": pid, "qty_milli": 24_000}]}).json()
    assert owner.post(f"/api/orders/{po['id']}/approve").status_code == 200
    return owner, po


def test_order_by_whatsapp_api_uploads_the_pdf_and_sends_it(make_client, net, monkeypatch):
    _configure(monkeypatch, po="purchase_order_v1")
    owner, po = _approved_order(make_client)
    assert owner.get(f"/api/orders/{po['id']}").json()["whatsapp_api"] is True
    r = owner.post(f"/api/orders/{po['id']}/send", {"channel": "whatsapp"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "sent" and out["sent_at"]
    assert out["sent_by"] == "whatsapp" and out["supplier"] == "Gupta Traders"
    assert out["sent_to"] == "+919822012345" and "whatsapp_link" not in out

    upload, message = net.requests
    assert upload.full_url == GRAPH + "/media"
    assert upload.get_header("Authorization") == "Bearer test-token-abc"
    assert upload.get_header("Content-type").startswith("multipart/form-data; boundary=")
    assert b'name="messaging_product"\r\n\r\nwhatsapp' in upload.data
    assert b'name="type"\r\n\r\napplication/pdf' in upload.data
    assert f'filename="{po["number"]}.pdf"'.encode() in upload.data and b"%PDF" in upload.data

    assert message.full_url == GRAPH + "/messages"
    assert message.get_header("Authorization") == "Bearer test-token-abc"
    assert message.get_header("Content-type") == "application/json"
    body = net.json()
    assert body["messaging_product"] == "whatsapp" and body["to"] == "919822012345"
    assert body["type"] == "template"
    t = body["template"]
    assert t["name"] == "purchase_order_v1" and t["language"] == {"code": "en"}
    header, main = t["components"]
    assert header == {"type": "header", "parameters": [{"type": "document", "document": {
        "id": "media-77", "filename": f"{po['number']}.pdf"}}]}
    texts = [p["text"] for p in main["parameters"]]
    assert texts[0] == po["number"] and "Andheri" in texts[1] and "excluding GST" in texts[2]

    sent = whatsapp.outbox[-1]
    assert sent.to == "+919822012345" and sent.document[0] == f"{po['number']}.pdf"
    log = owner.get("/api/activity?entity=purchase_order").json()
    assert "order.sent" in [e["action"] for e in log]

    # Without a template name it goes as a document with a caption (24-hour window).
    monkeypatch.delenv("DQ_WHATSAPP_TEMPLATE_PO")
    again = owner.post(f"/api/orders/{po['id']}/send", {"channel": "whatsapp"})
    assert again.status_code == 200, again.text
    body = net.json()
    assert body["type"] == "document"
    assert body["document"]["id"] == "media-77"
    assert body["document"]["caption"].startswith(f"Purchase order {po['number']} from Andheri")


def test_order_by_whatsapp_falls_back_to_a_link_when_not_set_up(make_client, net):
    owner, po = _approved_order(make_client)
    assert owner.get(f"/api/orders/{po['id']}").json()["whatsapp_api"] is False
    out = owner.post(f"/api/orders/{po['id']}/send", {"channel": "whatsapp"}).json()
    assert out["whatsapp_link"].startswith("https://wa.me/919822012345?text=Purchase%20order")
    assert out["status"] == "sent" and "sent_by" not in out
    assert net.requests == []                                  # nothing left the server


def test_order_by_whatsapp_refusals_are_said_not_swallowed(make_client, net, monkeypatch):
    _configure(monkeypatch)
    owner, po = _approved_order(make_client)
    net.fail = (400, {"message": "(#131047) Re-engagement message", "code": 131047})
    r = owner.post(f"/api/orders/{po['id']}/send", {"channel": "whatsapp"})
    assert r.status_code == 502
    assert "24 hours" in r.json()["error"] and "+919822012345" in r.json()["error"]
    assert owner.get(f"/api/orders/{po['id']}").json()["status"] == "approved"   # not sent
    # A number that cannot be a mobile is refused before anything is sent.
    net.fail = None
    n = len(net.requests)
    bad = owner.post(f"/api/orders/{po['id']}/send", {"channel": "whatsapp", "to": "12345"})
    assert bad.status_code == 422 and "not a mobile number" in bad.json()["error"]
    assert len(net.requests) == n


def test_order_without_a_supplier_phone_is_refused(make_client, net, monkeypatch):
    _configure(monkeypatch)
    owner, po = _approved_order(make_client, phone="")
    r = owner.post(f"/api/orders/{po['id']}/send", {"channel": "whatsapp"})
    assert r.status_code == 422 and "no phone number" in r.json()["error"]
    assert net.requests == []


# ================================================================= alerts

def _alert_world(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "Andheri"}).json()
    mona, ravi = make_client(), make_client()
    invite_and_join(owner, mona, "Mona Manager", "store_manager", [s1["id"]])
    invite_and_join(owner, ravi, "Ravi Manager", "store_manager", [s1["id"]])
    cid = owner.me["company"]["id"]
    with dbmod.SessionLocal() as db:
        for p in db.scalars(select(Person).where(Person.company_id == cid)):
            if p.name.startswith("Mona"):
                p.phone = "98200 11111"
            elif p.name.startswith("Ravi"):
                p.phone = "+91 98200 22222"
        db.commit()
    return owner, mona, ravi, cid, s1


def _raise(cid, store_id, *kinds):
    with dbmod.SessionLocal() as db:
        for k in kinds:
            db.add(Alert(company_id=cid, store_id=store_id, kind=k, severity="risk",
                         message=f"Something about {k} in Andheri.", dedupe_key=f"{k}:test",
                         link="#/stock"))
        db.commit()


def test_alert_settings_say_when_whatsapp_is_not_available(make_client, net, monkeypatch):
    owner, mona, ravi, cid, s1 = _alert_world(make_client)
    s = mona.get("/api/alerts/settings").json()
    assert s["whatsapp_available"] is False
    assert "not available on this server" in s["whatsapp_note"]
    _configure(monkeypatch)
    s = mona.get("/api/alerts/settings").json()
    assert s["whatsapp_available"] is True and "98200 11111" in s["whatsapp_note"]
    assert "no mobile number" in owner.get("/api/alerts/settings").json()["whatsapp_note"]


def test_alerts_by_whatsapp_respect_mutes_group_and_go_once(make_client, net, monkeypatch):
    _configure(monkeypatch, alert="dq_alert")
    owner, mona, ravi, cid, s1 = _alert_world(make_client)
    assert mona.put("/api/alerts/settings", {"whatsapp": True}).status_code == 200
    assert ravi.put("/api/alerts/settings", {"whatsapp": True,
                                             "muted": ["out_of_stock"]}).status_code == 200
    _raise(cid, s1["id"], "out_of_stock", "low_stock")
    with dbmod.SessionLocal() as db:
        assert A.deliver(db, cid) == 2                     # one message each
        db.commit()
    by_to = {m.to: m for m in whatsapp.outbox}
    assert set(by_to) == {"+919820011111", "+919820022222"}
    assert len(whatsapp.outbox) == 2                       # Mona's two came as one
    mona_msg = next(json.loads(r.data) for r in net.requests
                    if json.loads(r.data)["to"] == "919820011111")
    assert mona_msg["type"] == "template" and mona_msg["template"]["name"] == "dq_alert"
    name, text = [p["text"] for p in mona_msg["template"]["components"][0]["parameters"]]
    assert name == "Mona Manager"
    assert "Out of stock" in text and "Below target" in text and "\n" not in text
    ravi_text = by_to["+919820022222"].text
    assert "Below target" in ravi_text and "Out of stock" not in ravi_text   # muted
    # Not sent again on the next pass.
    with dbmod.SessionLocal() as db:
        assert A.deliver(db, cid) == 0
    assert len(whatsapp.outbox) == 2 and len(net.requests) == 2


def test_alerts_are_not_sent_by_whatsapp_when_not_set_up(make_client, net):
    owner, mona, ravi, cid, s1 = _alert_world(make_client)
    mona.put("/api/alerts/settings", {"whatsapp": True})
    _raise(cid, s1["id"], "out_of_stock")
    with dbmod.SessionLocal() as db:
        assert A.deliver(db, cid) == 0
        db.commit()
    assert whatsapp.outbox == [] and net.requests == []


def test_a_failed_whatsapp_alert_does_not_stop_the_rest(make_client, net, monkeypatch):
    _configure(monkeypatch)
    owner, mona, ravi, cid, s1 = _alert_world(make_client)
    mona.put("/api/alerts/settings", {"whatsapp": True, "email": True})
    _raise(cid, s1["id"], "out_of_stock")
    net.fail = (401, {"message": "Invalid OAuth access token.", "code": 190})
    with dbmod.SessionLocal() as db:
        assert A.deliver(db, cid) == 1                     # the email still went
        db.commit()
        assert db.scalar(select(Alert)).notified_at is not None


# ================================================================= receipts

def _paid_bill(make_client):
    owner = make_client()
    owner.signup()
    s1 = owner.post("/api/stores", {"code": "S1", "name": "Andheri", "mode": "connected"}).json()
    owner.import_file("products", "SKU,Name,Unit,MRP,Selling price,Cost,GST rate\n"
                                  "RICE,Rice 5 kg,each,500,450,380,5\n")
    cid = owner.me["company"]["id"]
    with dbmod.SessionLocal() as db:
        pid = db.scalar(select(Product)).id
        ledger.post(db, company_id=cid, store_id=s1["id"], product_id=pid, qty_milli=20_000,
                    type="opening")
        db.get(Company, cid).gstin = "27AAPFU0939F1ZV"
        db.commit()
    cashier = make_client()
    invite_and_join(owner, cashier, "Chirag Cashier", "cashier", [s1["id"]])
    r = cashier.post("/api/checkout/bills", {
        "store_id": s1["id"], "bill_number": "T1-0001", "device_id": "till-1",
        "lines": [{"product_id": pid, "qty_milli": 2000, "unit_price_paise": 45_000}],
        "payments": [{"method": "upi", "amount_paise": 90_000}]})
    assert r.status_code == 201, r.text
    return owner, cashier, s1, r.json()


def test_receipt_by_whatsapp_link_then_by_the_api(make_client, net, monkeypatch):
    owner, cashier, s1, bill = _paid_bill(make_client)
    url = f"/api/checkout/bills/{bill['id']}/receipt"
    assert cashier.get("/api/checkout/receipt-channels").json() == {"whatsapp": "link",
                                                                    "sms": False}
    out = cashier.post(url, {"channel": "whatsapp", "phone": "98200 12345"}).json()
    assert out["whatsapp_link"].startswith("https://wa.me/919820012345?text=")
    assert net.requests == []

    _configure(monkeypatch)
    assert cashier.get("/api/checkout/receipt-channels").json()["whatsapp"] == "api"
    r = cashier.post(url, {"channel": "whatsapp", "phone": "098200 12345"})
    assert r.status_code == 200, r.text
    assert r.json() == {"sent_by": "whatsapp", "sent_to": "+919820012345"}
    body = net.json()
    assert net.requests[-1].full_url == GRAPH + "/messages"
    assert body["to"] == "919820012345" and body["type"] == "text"
    text = body["text"]["body"]
    for part in ("Andheri", "GSTIN 27AAPFU0939F1ZV", "Bill T1-0001", "IST", "Rice 5 kg x 2",
                 "Total Rs. 900.00", "Paid by UPI"):
        assert part in text, (part, text)
    log = owner.get("/api/activity?entity=bill").json()
    sent = [e for e in log if e["action"] == "bill.receipt_sent"]
    assert sent and sent[0]["after"] == {"channel": "whatsapp", "to": "…2345"}   # not the number

    # With an approved receipt template, the facts go as its parameters.
    monkeypatch.setenv("DQ_WHATSAPP_TEMPLATE_RECEIPT", "dq_receipt")
    assert cashier.post(url, {"channel": "whatsapp", "phone": "9820012345"}).status_code == 200
    params = [p["text"] for p in net.json()["template"]["components"][0]["parameters"]]
    assert len(params) == 5 and params[1] == "T1-0001" and "UPI" in params[4]
    assert all("\n" not in p for p in params)


def test_receipt_by_sms(make_client, net, monkeypatch):
    owner, cashier, s1, bill = _paid_bill(make_client)
    url = f"/api/checkout/bills/{bill['id']}/receipt"
    r = cashier.post(url, {"channel": "sms", "phone": "9820012345"})
    assert r.status_code == 409 and "not set up" in r.json()["error"]
    assert sms.outbox == []
    monkeypatch.setenv("DQ_SMS_PROVIDER", "webhook")
    monkeypatch.setenv("DQ_SMS_WEBHOOK_URL", "https://sms.example/send")
    monkeypatch.setenv("DQ_SMS_WEBHOOK_TOKEN", "sms-secret")
    assert cashier.get("/api/checkout/receipt-channels").json()["sms"] is True
    r = cashier.post(url, {"channel": "sms", "phone": "+91 98200-12345"})
    assert r.status_code == 200, r.text
    assert r.json() == {"sent_by": "sms", "sent_to": "+919820012345"}
    req = net.requests[-1]
    assert req.full_url == "https://sms.example/send"
    assert req.get_header("Authorization") == "Bearer sms-secret"
    sent = json.loads(req.data)
    assert sent["to"] == "+919820012345" and "Bill T1-0001" in sent["text"]
    assert "₹" not in sent["text"]                       # keeps the SMS in the cheap alphabet


def test_receipt_refuses_a_bad_phone_and_other_companies(make_client, net, monkeypatch):
    _configure(monkeypatch)
    owner, cashier, s1, bill = _paid_bill(make_client)
    url = f"/api/checkout/bills/{bill['id']}/receipt"
    for bad in ("12345", "abcdefghij", "5555555555"):
        r = cashier.post(url, {"channel": "whatsapp", "phone": bad})
        assert r.status_code == 422, (bad, r.text)
    assert cashier.post(url, {"channel": "fax", "phone": "9820012345"}).status_code == 422
    assert net.requests == []
    other = make_client()
    other.signup(company="Other Mart", name="Omar Owner")
    r = other.post(url, {"channel": "whatsapp", "phone": "9820012345"})
    assert r.status_code == 404
    assert net.requests == []


def test_receipt_failures_are_an_error_answer(make_client, net, monkeypatch):
    _configure(monkeypatch)
    owner, cashier, s1, bill = _paid_bill(make_client)
    net.fail = (400, {"message": "Recipient phone number not in allowed list", "code": 131030})
    r = cashier.post(f"/api/checkout/bills/{bill['id']}/receipt",
                     {"channel": "whatsapp", "phone": "9820012345"})
    assert r.status_code == 502
    assert "allowed list" in r.json()["error"]
    assert [e for e in owner.get("/api/activity?entity=bill").json()
            if e["action"] == "bill.receipt_sent"] == []


def test_receipts_are_rate_limited_per_store(make_client, net, monkeypatch):
    _configure(monkeypatch)
    owner, cashier, s1, bill = _paid_bill(make_client)
    url = f"/api/checkout/bills/{bill['id']}/receipt"
    codes = [cashier.post(url, {"channel": "whatsapp", "phone": "9820012345"}).status_code
             for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429
