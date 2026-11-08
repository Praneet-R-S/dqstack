"""The role matrix from section 2, enforced on the server for every action.

    yes  = allowed in every store of the company
    own  = allowed only in the stores the person is assigned to
    no   = refused

The browser hides buttons a person cannot use, but that is a courtesy, not a
control: a Viewer who sends a request straight to the API is refused here, which is
acceptance scenario 16. Every endpoint that changes anything declares the action it
needs, and every store-level action is checked against the person's stores.
"""

from __future__ import annotations

YES, OWN, NO = "yes", "own", "no"

#            owner admin  store_mgr purchaser clerk cashier viewer
_MATRIX: dict[str, tuple[str, ...]] = {
    "company.settings":   (YES, YES, NO,  NO,  NO,  NO,  NO),
    "company.billing":    (YES, NO,  NO,  NO,  NO,  NO,  NO),
    "people.manage":      (YES, YES, NO,  NO,  NO,  NO,  NO),
    "imports.upload":     (YES, YES, OWN, NO,  NO,  NO,  NO),
    "forecast.review":    (YES, YES, OWN, OWN, NO,  NO,  NO),
    "orders.create":      (YES, YES, NO,  OWN, NO,  NO,  NO),
    "orders.approve":     (YES, YES, OWN, NO,  NO,  NO,  NO),
    "suppliers.manage":   (YES, YES, NO,  OWN, NO,  NO,  NO),
    "stock.receive":      (YES, YES, OWN, OWN, OWN, NO,  NO),
    "stock.transfer":     (YES, YES, OWN, NO,  OWN, NO,  NO),
    "stock.count":        (YES, YES, OWN, NO,  OWN, NO,  NO),
    "stock.approve_diff": (YES, YES, OWN, NO,  NO,  NO,  NO),
    "stock.wastage":      (YES, YES, OWN, NO,  OWN, NO,  NO),
    "stock.opening":      (YES, YES, OWN, NO,  NO,  NO,  NO),
    "sales.bill":         (YES, YES, OWN, NO,  NO,  OWN, NO),
    "sales.refund":       (YES, YES, OWN, NO,  NO,  NO,  NO),
    "sales.enter":        (YES, YES, OWN, NO,  NO,  NO,  NO),
    "day.close":          (YES, YES, OWN, NO,  NO,  NO,  NO),
    "reports.view":       (YES, YES, OWN, OWN, NO,  NO,  OWN),
    "activity.view":      (YES, YES, OWN, NO,  NO,  NO,  OWN),
    "integrations.manage": (YES, YES, NO, NO,  NO,  NO,  NO),
    # Reading operational data in your stores. Everyone has it for their own stores.
    "data.read":          (YES, YES, OWN, OWN, OWN, OWN, OWN),
}

_INDEX = {r: i for i, r in enumerate(
    ("owner", "admin", "store_manager", "purchaser", "stock_clerk", "cashier", "viewer"))}

ACTIONS = tuple(_MATRIX)


def level(role: str, action: str) -> str:
    if action not in _MATRIX:
        raise KeyError(f"unknown action {action!r}")
    i = _INDEX.get(role)
    return NO if i is None else _MATRIX[action][i]


def allowed(role: str, action: str, store_id: str | None, store_ids: set[str]) -> bool:
    """Whether a person may do `action`, optionally in a particular store.

    `store_ids` is the set of stores the person is assigned to. For a company-wide
    action (store_id None) an `own` grant still allows the action, scoped later to
    their stores by the query itself."""
    lv = level(role, action)
    if lv == YES:
        return True
    if lv == NO:
        return False
    return store_id is None or store_id in store_ids


def sees_all_stores(role: str) -> bool:
    return role in ("owner", "admin")


def matrix() -> dict[str, dict[str, str]]:
    """For the settings screen and the tests: the whole table, by role."""
    return {a: {r: _MATRIX[a][i] for r, i in _INDEX.items()} for a in _MATRIX}
