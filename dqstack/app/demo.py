"""A demonstration company with realistic data, for trying the app and for sales demos.

    python -m dqstack.app.manage seed-demo --email you@example.com

Makes "Sharma Supermarkets (demo)": two stores (Pune runs Checkout, Kochi uploads its
daily file), 30 products in six categories with barcodes and GST rates, two
suppliers with lead times, order days and minimums, about 11 months of daily sales
with a weekly pattern and festival lifts, opening stock, a team of five, and a first
forecast. The figures are synthetic and labelled as such everywhere they appear.
Passwords are taken from DQ_DEMO_PASSWORD (or the command line prompt), never
hard-coded.
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import festivals, ledger
from .db import utcnow
from .models import (Barcode, CalendarEvent, Company, DailySales, Person, Product, Store,
                     Supplier, SupplierProduct, assign_stores)
from .security import hash_password

CATALOGUE = [
    # sku, name, category, unit, mrp, price, cost, gst%, case, daily level, perishable
    ("RICE-SNA-5", "Sona masoori rice 5 kg", "Rice and grains", "each", 450, 430, 365, 5, 4, 7.0, False),
    ("RICE-BAS-1", "Basmati rice 1 kg", "Rice and grains", "each", 160, 150, 118, 5, 10, 9.0, False),
    ("ATTA-10", "Whole wheat atta 10 kg", "Rice and grains", "each", 520, 499, 430, 5, 4, 3.5, False),
    ("DAL-TOOR-1", "Toor dal 1 kg", "Pulses", "each", 185, 175, 148, 5, 10, 8.0, False),
    ("DAL-MOONG-1", "Moong dal 1 kg", "Pulses", "each", 160, 150, 126, 5, 10, 4.0, False),
    ("CHANA-1", "Kabuli chana 1 kg", "Pulses", "each", 140, 132, 108, 5, 10, 3.0, False),
    ("OIL-SUN-1", "Sunflower oil 1 L", "Oils", "each", 165, 155, 134, 5, 12, 10.0, False),
    ("OIL-GN-1", "Groundnut oil 1 L", "Oils", "each", 210, 199, 172, 5, 12, 4.0, False),
    ("GHEE-500", "Ghee 500 ml", "Oils", "each", 340, 325, 279, 12, 12, 3.0, False),
    ("SUGAR-1", "Sugar 1 kg", "Staples", "each", 50, 46, 40, 5, 20, 14.0, False),
    ("SALT-1", "Iodised salt 1 kg", "Staples", "each", 28, 26, 19, 0, 25, 6.0, False),
    ("TEA-500", "Tea 500 g", "Staples", "each", 280, 265, 214, 5, 12, 5.0, False),
    ("ONION", "Onions", "Vegetables", "kg", 45, 40, 30, 0, 1, 18.0, True),
    ("POTATO", "Potatoes", "Vegetables", "kg", 38, 34, 25, 0, 1, 15.0, True),
    ("TOMATO", "Tomatoes", "Vegetables", "kg", 48, 42, 30, 0, 1, 12.0, True),
    ("MILK-500", "Toned milk 500 ml", "Dairy", "each", 30, 30, 26, 0, 24, 40.0, True),
    ("CURD-400", "Curd 400 g", "Dairy", "each", 45, 45, 37, 5, 12, 12.0, True),
    ("PANEER-200", "Paneer 200 g", "Dairy", "each", 95, 90, 74, 5, 12, 6.0, True),
    ("BREAD", "Sandwich bread", "Bakery", "each", 45, 45, 36, 0, 10, 14.0, True),
    ("BISC-GLU", "Glucose biscuits", "Snacks", "each", 10, 10, 8, 18, 48, 30.0, False),
    ("NAMK-200", "Bhujia 200 g", "Snacks", "each", 55, 52, 40, 12, 24, 8.0, False),
    ("CHIPS-50", "Potato chips 50 g", "Snacks", "each", 20, 20, 15, 12, 48, 16.0, False),
    ("SWEET-BOX", "Kaju katli 500 g", "Snacks", "each", 480, 460, 360, 5, 6, 1.0, True),
    ("SOAP-100", "Bath soap 100 g", "Personal care", "each", 45, 42, 32, 18, 36, 9.0, False),
    ("PASTE-150", "Toothpaste 150 g", "Personal care", "each", 105, 99, 76, 18, 24, 4.0, False),
    ("SHAMPOO-180", "Shampoo 180 ml", "Personal care", "each", 175, 165, 128, 18, 24, 2.5, False),
    ("DET-1", "Detergent powder 1 kg", "Home care", "each", 130, 125, 98, 18, 12, 5.0, False),
    ("DISH-500", "Dishwash liquid 500 ml", "Home care", "each", 110, 105, 80, 18, 12, 4.0, False),
    ("DIYA-12", "Clay diyas, pack of 12", "Festival", "each", 120, 110, 70, 5, 10, 0.4, False),
    ("AGARBATTI", "Agarbatti", "Festival", "each", 50, 48, 32, 5, 24, 3.0, False),
]
WEEKLY = [0.95, 0.9, 0.92, 0.97, 1.05, 1.3, 1.25]


def _ean(n: int) -> str:
    body = f"890{n:09d}"
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


def _poisson(rng: random.Random, mu: float) -> int:
    if mu > 40:
        return max(0, int(round(rng.gauss(mu, math.sqrt(mu)))))
    L, k, p = math.exp(-mu), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def seed(db: Session, owner_email: str, password: str, days: int = 330) -> Company:
    if db.scalar(select(Person.id).where(Person.email == owner_email.lower())):
        raise ValueError("Someone with that email already has an account.")
    co = Company(name="Sharma Supermarkets (demo)", timezone="Asia/Kolkata", plan="demo",
                 setup={"company": utcnow().isoformat(), "calendar": utcnow().isoformat(),
                        "rules": utcnow().isoformat(), "readiness": utcnow().isoformat(),
                        "go_live": utcnow().isoformat()})
    db.add(co)
    db.flush()
    festivals.seed(db, co.id)
    pune = Store(company_id=co.id, code="PUN1", name="Pune, Kothrud", state="Maharashtra",
                 address="12 Karve Road, Kothrud, Pune 411038", day_close_time="22:00",
                 mode="connected", size="large")
    kochi = Store(company_id=co.id, code="KOC1", name="Kochi, MG Road", state="Kerala",
                  address="MG Road, Ernakulam, Kochi 682011", day_close_time="21:30",
                  weekly_off=[], mode="standalone", size="medium")
    db.add_all([pune, kochi])
    db.flush()
    pw = hash_password(password)
    team = [("Ravi Sharma", owner_email.lower(), "owner", []),
            ("Meera Iyer", "meera@demo.dqstack.local", "store_manager", [kochi]),
            ("Arjun Patil", "arjun@demo.dqstack.local", "store_manager", [pune]),
            ("Puja Nair", "puja@demo.dqstack.local", "purchaser", [pune, kochi]),
            ("Kabir Khan", "kabir@demo.dqstack.local", "stock_clerk", [pune]),
            ("Chirag Desai", "chirag@demo.dqstack.local", "cashier", [pune])]
    for name, email, role, stores in team:
        p = Person(company_id=co.id, name=name, email=email, role=role, status="active",
                   password_hash=pw, manager_pin_hash=hash_password("2580")
                   if role in ("owner", "store_manager") else None)
        db.add(p)
        db.flush()
        assign_stores(db, p, stores)
    gupta = Supplier(company_id=co.id, name="Gupta Traders", email="orders@gupta.example",
                     phone="9822012345", order_days=[0, 3], lead_time_days=2,
                     min_order_paise=500_000)
    fresh = Supplier(company_id=co.id, name="Fresh Farms", email="orders@freshfarms.example",
                     phone="9846012345", order_days=[], lead_time_days=1, min_order_paise=0)
    db.add_all([gupta, fresh])
    db.flush()
    products = []
    for i, (sku, name, cat, unit, mrp, price, cost, gst, case, _, perish) in enumerate(CATALOGUE):
        p = Product(company_id=co.id, sku=sku, name=name, category=cat, unit=unit,
                    mrp_paise=mrp * 100, price_paise=price * 100, cost_paise=cost * 100,
                    gst_rate_bp=gst * 100, case_pack=case, perishable=perish,
                    shelf_life_days=5 if perish else 365, status="active")
        db.add(p)
        db.flush()
        if unit == "each":
            db.add(Barcode(company_id=co.id, product_id=p.id, code=_ean(100 + i)))
        sup = fresh if cat in ("Vegetables", "Dairy", "Bakery") else gupta
        db.add(SupplierProduct(company_id=co.id, supplier_id=sup.id, product_id=p.id,
                               cost_paise=cost * 100, case_pack=case))
        products.append(p)
    db.flush()
    # ---- sales history with weekly pattern, festival lifts and a slow trend
    rng = random.Random(20261109)
    end = date.today() - timedelta(days=1)
    lifts: dict[date, float] = {}
    for e in db.scalars(select(CalendarEvent).where(CalendarEvent.company_id == co.id,
                                                    CalendarEvent.kind == "festival")):
        big = e.name in ("Diwali", "Dhanteras", "Onam", "Ganesh Chaturthi", "Dussehra", "Holi",
                         "Eid al-Fitr")
        for k in range(-6, 2):
            d = e.day + timedelta(days=k)
            lifts[d] = max(lifts.get(d, 1.0), (1.6 if big else 1.15) if k <= 0 else 0.85)
    rows = []
    for s_i, store in enumerate((pune, kochi)):
        size = 1.0 if s_i == 0 else 0.65
        for p, spec in zip(products, CATALOGUE):
            level = spec[9] * size
            festive = spec[2] in ("Festival", "Snacks", "Oils")
            for d in range(days):
                day = end - timedelta(days=days - 1 - d)
                mu = level * WEEKLY[day.weekday()] * (1 + 0.15 * d / days)
                lift = lifts.get(day, 1.0)
                mu *= lift if festive or lift < 1 else (1 + (lift - 1) / 3)
                if spec[2] == "Festival":
                    mu *= 6.0 if lift > 1.3 else 0.5
                units = _poisson(rng, mu)
                # Loose produce sells in uneven weights; packed goods in whole units.
                qty = units * 1000 if spec[3] == "each" else int(units * 1000 * rng.uniform(0.8, 1.2))
                rows.append(DailySales(company_id=co.id, store_id=store.id, product_id=p.id,
                                       day=day, units_milli=qty,
                                       revenue_paise=qty * spec[5] * 100 // 1000,
                                       posted_milli=0, source="history"))
    db.add_all(rows)
    db.flush()
    # ---- opening stock, set three days ago, for every product but two
    at = datetime.now(timezone.utc) - timedelta(days=3)
    for store in (pune, kochi):
        for p, spec in zip(products, CATALOGUE):
            if p.sku in ("SWEET-BOX", "SHAMPOO-180") and store is kochi:
                continue
            units = max(0, int(spec[9] * rng.uniform(2, 14)))
            if p.sku == "BREAD":
                units = 0
            if units == 0:
                row = ledger._soh(db, co.id, store.id, p.id)       # known, and empty
                ledger.mark_known(row, at)
                ledger.baselined(row, at)
                row.opening_milli = 0
                continue
            ledger.post(db, company_id=co.id, store_id=store.id, product_id=p.id,
                        qty_milli=units * 1000, type="opening", unit_cost_paise=p.cost_paise,
                        reason="Opening stock (demo)", at=at)
    db.flush()
    return co
