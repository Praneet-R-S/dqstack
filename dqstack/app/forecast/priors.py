"""National festival priors: what a festival does to sales before a company's own
sales have shown it (specification: "Only 2 to 11 months of history -- festivals not
yet seen are taken from the national festival effect for that category, and flagged
lower confidence").

Where the numbers come from. They are the vendor's own assumptions, never another
client's data: the festival lifts the demo company is generated with
(`dqstack/app/demo.py`, `seed`), written down here as constants and turned into
multipliers by `_derive`:

* a major festival (MAJOR below) lifts sales 1.6 times on the festival day and the
  six days before it; any other national-list festival 1.15 times;
* festive categories (snacks, sweets, oils) take the whole lift; everyday
  categories a third of it;
* festival goods (diyas, pooja items) sell six times their normal rate around a
  major festival and half of it otherwise -- 19.2 times their usual level.

A first festival in a company's data is a guess, so the prior is conservative:
half the generator's extra demand (SHRINK), and never more than three times the
forecast (CAP). The day after a festival, which the generator dips to 0.85, is left
alone: cutting stock on a guess is the riskier mistake.

    PRIOR = {"major": {"festival_goods": 3.0, "festive": 1.3, "everyday": 1.1},
             "minor": {"festival_goods": 1.075, "festive": 1.075, "everyday": 1.025}}

Where it applies. The full engine learns a festival from the company's own sales
once it has seen it; the prior is applied only to festivals with no occurrence in
the company's history. The early forecast learns no festivals at all, so it gets
the prior for every festival, flagged the same way. Only festivals on the national
list (`app/festivals.py`) have a prior; a company's own local events do not.
"""

from __future__ import annotations

import re
from datetime import timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import HORIZON
from .. import festivals as fest_list
from ..models import CalendarEvent

# ---- the demo generator's assumptions (app/demo.py, seed)
GEN_MAJOR_LIFT = 1.6
GEN_MINOR_LIFT = 1.15
GEN_EVERYDAY_SHARE = 1 / 3
GEN_FESTIVAL_GOODS = (6.0, 0.5)       # times the level in a major window, otherwise
MAJOR = ("Diwali", "Dhanteras", "Onam", "Ganesh Chaturthi", "Dussehra", "Holi",
         "Eid al-Fitr")
WINDOW_BEFORE = 6                     # the festival day and the six days before it

SHRINK = 0.5
CAP = 3.0

# Category names are the company's own words, so groups are found by keyword at
# the start of a word ("Oils" and "Cooking oil" match "oil"; "Toiletries" does not).
GROUP_KEYWORDS = {
    "festival_goods": ("festival", "festive", "pooja", "puja", "diya", "rangoli",
                       "decorat", "gift", "cracker"),
    "festive": ("snack", "namkeen", "sweet", "mithai", "confection", "chocolate",
                "dry fruit", "dryfruit", "nuts", "oil", "ghee"),
}
GROUPS = ("festival_goods", "festive", "everyday")


def _relative(kind: str, group: str) -> float:
    lift = GEN_MAJOR_LIFT if kind == "major" else GEN_MINOR_LIFT
    if group == "festive":
        return lift
    if group == "everyday":
        return 1 + (lift - 1) * GEN_EVERYDAY_SHARE
    high, usual = GEN_FESTIVAL_GOODS
    return lift * (high if lift > 1.3 else usual) / usual


def _derive() -> dict[str, dict[str, float]]:
    return {kind: {g: round(min(1 + SHRINK * (_relative(kind, g) - 1), CAP), 4)
                   for g in GROUPS} for kind in ("major", "minor")}


PRIOR = _derive()
NATIONAL_FESTIVALS = frozenset(e["name"] for e in fest_list.events() if e["kind"] == "festival")


def group_of(category: str | None) -> str:
    words = " ".join(re.findall(r"[a-z]+", (category or "").lower()))
    for group, keys in GROUP_KEYWORDS.items():
        if any(re.search(r"\b" + re.escape(k), words) for k in keys):
            return group
    return "everyday"


def multiplier(name: str, category: str | None) -> float:
    return PRIOR["major" if name in MAJOR else "minor"][group_of(category)]


def _applies(e: CalendarEvent, sid: str, store) -> bool:
    if e.store_ids:
        return sid in e.store_ids
    return not e.states or bool(store.state and store.state in e.states)


def apply(db: Session, company_id: str, res, data, tier: str
          ) -> dict[str, list[tuple]]:
    """Multiply the forecast in `res.quantiles` (in place) on the windows of upcoming
    national festivals the company's own sales have not shown -- every upcoming
    festival for the early forecast -- and return, per store, the festivals applied:
    [(day, name, basis)], basis "first" (not in this company's sales yet) or
    "national" (seen, but the early forecast does not learn festivals)."""
    cutoff = data.cutoff
    last = cutoff + timedelta(days=HORIZON + WINDOW_BEFORE)
    upcoming = [e for e in db.scalars(select(CalendarEvent).where(
        CalendarEvent.company_id == company_id, CalendarEvent.kind == "festival",
        CalendarEvent.day > cutoff, CalendarEvent.day <= last)
        .order_by(CalendarEvent.day, CalendarEvent.name)) if e.name in NATIONAL_FESTIVALS]
    if not upcoming:
        return {}
    seen = set(db.scalars(select(CalendarEvent.name).where(
        CalendarEvent.company_id == company_id, CalendarEvent.kind == "festival",
        CalendarEvent.day >= data.start, CalendarEvent.day <= cutoff)))
    q = res.quantiles
    S, H = q.shape[0], q.shape[1]
    series = res.P.series
    store = series["store_id"].astype(str).to_numpy()
    items = data.tables["items"]
    cat_of = dict(zip(items["item_id"].astype(str), items["cat_id"].astype(str)))
    by_cat = {c: group_of(c) for c in set(cat_of.values())}
    group = np.array([by_cat.get(cat_of.get(i), "everyday")
                      for i in series["item_id"].astype(str)])
    mult = np.ones((S, H), dtype="float64")
    notes: dict[str, list[tuple]] = {}
    in_store = {sid: store == sid for sid in data.stores}
    for e in upcoming:
        unseen = e.name not in seen
        if not unseen and tier != "starter":
            continue                    # the engine has learned this one from the data
        h0 = max(1, (e.day - cutoff).days - WINDOW_BEFORE)
        h1 = min(H, (e.day - cutoff).days)
        if h1 < h0:
            continue
        kind = "major" if e.name in MAJOR else "minor"
        for sid, st in data.stores.items():
            if not _applies(e, sid, st):
                continue
            rows = in_store[sid]
            if not rows.any():
                continue
            for g in GROUPS:
                sel = np.flatnonzero(rows & (group == g))
                if len(sel):
                    block = mult[sel, h0 - 1:h1]
                    mult[sel, h0 - 1:h1] = np.maximum(block, PRIOR[kind][g])
            notes.setdefault(sid, []).append((e.day, e.name, "first" if unseen else "national"))
    q *= mult[:, :, None].astype(q.dtype)
    return notes
