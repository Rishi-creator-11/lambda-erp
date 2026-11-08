#!/usr/bin/env python3
"""Inventory Ops Copilot: reorder proposal, explicit confirmation, isolation, audit.

Run: python -m tests.test_inventory_ops
"""

import os
import sys

from fastapi import HTTPException

from lambda_erp.database import setup
from lambda_erp.hooks import clear_hooks

SID_A = "sess_AAAAAAAAAAAAAAAA"
SID_B = "sess_BBBBBBBBBBBBBBBB"


def fresh():
    from api import services
    from inventory_ops import plugin
    from inventory_ops.seed import seed

    clear_hooks()
    services.REGISTERED_ACTIONS.clear()
    services._PLUGIN_TABLES.clear()
    db = setup(":memory:")
    # core tables already exist from setup(); plugin tables are created like the app lifespan does
    plugin.register()
    services.apply_plugin_schema()
    seed()
    return db, services


def codes(state, key="is_low"):
    return {r["item_code"] for r in state["items"] if r[key]}


def check():
    from inventory_ops import service

    db, services = fresh()

    # --- reads: policy vs Bin, open PO counted as incoming
    st = service.low_stock()
    assert codes(st) == {"PAP-100G-A3", "INK-CYN-1L", "PKG-MAIL-M", "LAM-FILM-GL"}, codes(st)
    mag = next(r for r in st["items"] if r["item_code"] == "INK-MAG-1L")
    assert mag["on_hand"] == 2 and mag["on_order"] == 6 and not mag["is_low"], mag

    # --- propose: quantities, pack rounding, justification; writes no purchasing docs
    po_before = db.sql('SELECT COUNT(*) c FROM "Purchase Order"')[0]["c"]
    p = service.propose(user=None, session_id=SID_A, scope="sandbox")
    q = {l["item_code"]: l["qty"] for l in p["lines"]}
    assert q == {"PAP-100G-A3": 52, "INK-CYN-1L": 15, "PKG-MAIL-M": 700, "LAM-FILM-GL": 11}, q
    assert all("on hand" in l["justification"] and "reorder level" in l["justification"] for l in p["lines"])
    assert db.sql('SELECT COUNT(*) c FROM "Purchase Order"')[0]["c"] == po_before
    again = service.propose(user=None, session_id=SID_A, scope="sandbox")
    assert again["id"] == p["id"], "identical open proposal must be idempotent"

    # --- explicit confirmation required
    for bad in (None, False, "true", 1):
        try:
            service.confirm_sandbox(session_id=SID_A, proposal_id=p["id"], confirm=bad)
            raise AssertionError(f"confirm={bad!r} accepted")
        except service.ReorderError as exc:
            assert "Explicit confirmation" in str(exc)
    assert service.get_proposal(p["id"], session_id=SID_A, scope="sandbox")["status"] == "proposed"

    # --- another session cannot see or confirm it
    try:
        service.confirm_sandbox(session_id=SID_B, proposal_id=p["id"], confirm=True)
        raise AssertionError("cross-session confirm worked")
    except service.NotFound:
        pass

    # --- confirm: before/after, shared ERP untouched
    shared_po = db.sql('SELECT COUNT(*) c FROM "Purchase Order"')[0]["c"]
    shared_bin = db.sql('SELECT SUM(ordered_qty) s FROM "Bin"')[0]["s"]
    res = service.confirm_sandbox(session_id=SID_A, proposal_id=p["id"], confirm=True)
    assert res["mode"] == "sandbox" and res["order_ref"].startswith("SBX-")
    assert codes(res["before"]) and not codes(res["after"]), (codes(res["before"]), codes(res["after"]))
    assert db.sql('SELECT COUNT(*) c FROM "Purchase Order"')[0]["c"] == shared_po
    assert db.sql('SELECT SUM(ordered_qty) s FROM "Bin"')[0]["s"] == shared_bin
    assert codes(service.low_stock(SID_B)) == {"PAP-100G-A3", "INK-CYN-1L", "PKG-MAIL-M", "LAM-FILM-GL"}, "other session must be unaffected"

    # --- duplicate confirmation is refused and does not write again
    try:
        service.confirm_sandbox(session_id=SID_A, proposal_id=p["id"], confirm=True)
        raise AssertionError("duplicate confirm accepted")
    except service.Conflict as exc:
        assert "Duplicate confirmation" in str(exc) and res["order_ref"] in str(exc)
    assert db.sql('SELECT COUNT(*) c FROM "Reorder Sandbox Order" WHERE session_id=?', [SID_A])[0]["c"] == 1

    # --- nothing left to reorder after confirming
    try:
        service.propose(user=None, session_id=SID_A, scope="sandbox")
        raise AssertionError("expected nothing-to-reorder")
    except service.ReorderError as exc:
        assert "Nothing is at or below" in str(exc)

    # --- caps: per-session order cap
    os.environ["INVENTORY_OPS_SANDBOX_ORDERS_PER_SESSION"] = "1"
    p2 = service.propose(user=None, session_id=SID_B, scope="sandbox")
    service.confirm_sandbox(session_id=SID_B, proposal_id=p2["id"], confirm=True)  # B's first order is fine
    del os.environ["INVENTORY_OPS_SANDBOX_ORDERS_PER_SESSION"]

    # --- real path: viewer refused by upstream gate; manager creates + submits real POs through hooks
    manager = {"role": "manager", "email": "ops.manager@example.test", "name": "USR-MGR"}
    viewer = {"role": "viewer", "email": "viewer@example.test", "name": "USR-VIEW"}
    rp = services.run_registered_action("propose_reorder", {}, viewer)  # proposing is read-only
    assert rp["scope"] == "erp" and rp["status"] == "proposed"
    try:
        services.run_registered_action("confirm_reorder", {"proposal_id": rp["id"], "confirm": True}, viewer)
        raise AssertionError("viewer wrote")
    except PermissionError:
        pass
    assert db.sql('SELECT COUNT(*) c FROM "Purchase Order"')[0]["c"] == shared_po
    try:
        services.run_registered_action("confirm_reorder", {"proposal_id": rp["id"], "confirm": True}, {"role": "public_manager"})
        raise AssertionError("shared demo account wrote")
    except PermissionError:
        pass
    try:
        services.run_registered_action("confirm_reorder", {"proposal_id": rp["id"]}, manager)
        raise AssertionError("missing confirm accepted")
    except service.ReorderError:
        pass
    out = services.run_registered_action("confirm_reorder", {"proposal_id": rp["id"], "confirm": True}, manager)
    assert out["mode"] == "real" and len(out["purchase_orders"]) == 3, out  # one PO per supplier
    pos = db.sql('SELECT name, docstatus FROM "Purchase Order" WHERE name IN (%s)' % ",".join("?" * 3), out["purchase_orders"])
    assert all(r["docstatus"] == 1 for r in pos), pos
    assert not codes(out["after"]) and codes(out["before"])
    try:
        services.run_registered_action("confirm_reorder", {"proposal_id": rp["id"], "confirm": True}, manager)
        raise AssertionError("duplicate real confirm accepted")
    except service.Conflict:
        pass
    assert db.sql('SELECT COUNT(*) c FROM "Purchase Order"')[0]["c"] == shared_po + 3

    # --- audit: events recorded, including the PO hook
    events = {e["event"] for e in service.audit_events(limit=200)}
    assert {"proposed", "confirmed_sandbox", "confirmed_real", "purchase_order_submitted"} <= events, events
    hook_events = [e for e in service.audit_events(limit=200) if e["event"] == "purchase_order_submitted" and e["actor"] == manager["email"]]
    assert len(hook_events) == 3, hook_events


def check_router():
    from fastapi.testclient import TestClient

    from inventory_ops.router import router
    from fastapi import FastAPI

    fresh()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    c = TestClient(app)
    assert c.get("/api/inventory-ops/state", params={"session_id": "short"}).status_code == 422
    sid = "sess_ROUTERTESTSESS1"
    assert c.get("/api/inventory-ops/state", params={"session_id": sid}).json()["stock"]["low_count"] == 4
    prop = c.post("/api/inventory-ops/sandbox/propose", json={"session_id": sid}).json()
    r = c.post("/api/inventory-ops/sandbox/confirm", json={"session_id": sid, "proposal_id": prop["id"]})
    assert r.status_code == 422 and "Explicit confirmation" in r.json()["detail"], r.text
    ok = c.post("/api/inventory-ops/sandbox/confirm", json={"session_id": sid, "proposal_id": prop["id"], "confirm": True})
    assert ok.status_code == 200, ok.text
    dup = c.post("/api/inventory-ops/sandbox/confirm", json={"session_id": sid, "proposal_id": prop["id"], "confirm": True})
    assert dup.status_code == 409, dup.text
    # a visitor trying the real write is refused and audited
    real = c.post("/api/inventory-ops/real/confirm", json={"proposal_id": prop["id"], "confirm": True, "session_id": sid})
    assert real.status_code == 401, real.text
    from inventory_ops import service
    ev = {e["event"] for e in service.audit_events(session_id=sid, limit=100)}
    assert {"denied_unauthenticated", "denied_duplicate", "confirmed_sandbox", "proposed"} <= ev, ev


def main():
    print("Inventory Ops checks")
    check()
    check_router()
    print("All Inventory Ops checks passed.")


if __name__ == "__main__":
    sys.exit(main())
