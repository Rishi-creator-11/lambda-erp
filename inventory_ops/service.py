"""Reorder workflow: read inventory, propose, confirm once, audit everything.

Two write paths share the same proposal/confirm contract:
  * real    - manager/admin through the upstream action gate; creates and submits
              Purchase Orders via Lambda ERP's own document lifecycle (hooks fire).
  * sandbox - anonymous public visitors; writes only to the session-scoped
              "Reorder Sandbox Order" table, never to shared ERP documents.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from datetime import datetime, timezone

from lambda_erp.database import get_db
from lambda_erp.utils import nowdate


class ReorderError(Exception):
    status = 422


class NotFound(ReorderError):
    status = 404


class Conflict(ReorderError):
    status = 409


class CapExceeded(ReorderError):
    status = 429


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _caps():
    return {
        "per_session": int(os.environ.get("INVENTORY_OPS_SANDBOX_ORDERS_PER_SESSION", "2")),
        "per_hour": int(os.environ.get("INVENTORY_OPS_SANDBOX_ORDERS_PER_HOUR", "60")),
        "proposals_per_session": int(os.environ.get("INVENTORY_OPS_SANDBOX_PROPOSALS_PER_SESSION", "6")),
    }


# --- audit -----------------------------------------------------------------------------


def audit(event: str, *, actor=None, role=None, session_id=None, proposal_id=None, detail=None, db=None) -> None:
    db = db or get_db()
    db.sql(
        'INSERT INTO "Reorder Audit Event" (id, ts, actor, role, session_id, event, proposal_id, detail) VALUES (?,?,?,?,?,?,?,?)',
        [uuid.uuid4().hex, _now(), actor, role, session_id, event, proposal_id, json.dumps(detail, sort_keys=True) if detail is not None else None],
    )
    db.conn.commit()


def audit_events(*, session_id=None, proposal_id=None, limit=50) -> list[dict]:
    db = get_db()
    where, vals = [], []
    if session_id:
        where.append("session_id = ?")
        vals.append(session_id)
    if proposal_id:
        where.append("proposal_id = ?")
        vals.append(proposal_id)
    q = 'SELECT ts, actor, role, event, proposal_id, detail FROM "Reorder Audit Event"'
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY ts DESC, id LIMIT ?"
    rows = db.sql(q, vals + [limit])
    return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]


def audit_po_submit(doc, *args, **kwargs) -> None:
    """Hook: record every Purchase Order submission (including ones made outside this plugin)."""
    from api.write_context import current_write_actor

    actor = current_write_actor() or {}
    audit(
        "purchase_order_submitted",
        actor=actor.get("email") or actor.get("name"),
        role=actor.get("role"),
        detail={"po": doc.name, "supplier": doc.get("supplier")},
    )


# --- reads -----------------------------------------------------------------------------


def _sandbox_on_order(db, session_id: str | None) -> dict[str, float]:
    if not session_id:
        return {}
    out: dict[str, float] = {}
    for row in db.sql('SELECT lines_json FROM "Reorder Sandbox Order" WHERE session_id = ?', [session_id]):
        for ln in json.loads(row["lines_json"]):
            out[ln["item_code"]] = out.get(ln["item_code"], 0.0) + float(ln["qty"])
    return out


def low_stock(session_id: str | None = None) -> dict:
    """Inventory vs reorder policy. Open POs and this session's sandbox orders count as incoming."""
    db = get_db()
    sandbox = _sandbox_on_order(db, session_id)
    rows = []
    for p in db.sql('SELECT * FROM "Reorder Policy" ORDER BY item_code'):
        b = db.sql('SELECT actual_qty, ordered_qty FROM "Bin" WHERE item_code = ? AND warehouse = ?', [p["item_code"], p["warehouse"]])
        on_hand = float(b[0]["actual_qty"]) if b else 0.0
        on_order = float(b[0]["ordered_qty"]) if b else 0.0
        sb = sandbox.get(p["item_code"], 0.0)
        available = on_hand + on_order + sb
        low = available <= p["reorder_level"]
        qty = 0.0
        if low:
            need = p["target_level"] - available
            qty = math.ceil(need / (p["pack_size"] or 1)) * (p["pack_size"] or 1)
        name = db.get_value("Item", p["item_code"], "item_name")
        rows.append({
            "item_code": p["item_code"], "item_name": name, "warehouse": p["warehouse"],
            "on_hand": on_hand, "on_order": on_order, "sandbox_on_order": sb, "available": available,
            "reorder_level": p["reorder_level"], "target_level": p["target_level"],
            "supplier": p["supplier"], "unit_cost": p["unit_cost"], "pack_size": p["pack_size"],
            "is_low": low, "suggested_qty": qty, "line_total": round(qty * p["unit_cost"], 2),
        })
    return {"items": rows, "low_count": sum(1 for r in rows if r["is_low"])}


# --- propose ---------------------------------------------------------------------------


def _justification(r: dict) -> str:
    return (
        f"{r['item_name']} ({r['item_code']}): {r['on_hand']:g} on hand + {r['on_order'] + r['sandbox_on_order']:g} on order "
        f"= {r['available']:g}, at or below reorder level {r['reorder_level']:g}. Ordering {r['suggested_qty']:g} "
        f"(packs of {r['pack_size']:g}) brings projected stock to about target {r['target_level']:g}; "
        f"{r['supplier']} at {r['unit_cost']:.2f} each = {r['line_total']:.2f}."
    )


def _fingerprint(scope: str, session_id: str | None, lines: list[dict]) -> str:
    key = json.dumps([scope, session_id, sorted((l["item_code"], l["qty"], l["supplier"]) for l in lines)])
    return hashlib.sha256(key.encode()).hexdigest()


def propose(*, user: dict | None, session_id: str | None, scope: str) -> dict:
    """Build (and store) a draft reorder. Writes nothing to purchasing documents."""
    db = get_db()
    if scope == "sandbox":
        caps = _caps()
        n = db.sql('SELECT COUNT(*) AS c FROM "Reorder Proposal" WHERE session_id = ?', [session_id])[0]["c"]
        if n >= caps["proposals_per_session"]:
            raise CapExceeded("Sandbox proposal limit reached for this session.")
    state = low_stock(session_id if scope == "sandbox" else None)
    lows = [r for r in state["items"] if r["is_low"]]
    if not lows:
        raise ReorderError("Nothing is at or below its reorder level.")
    lines = [
        {"item_code": r["item_code"], "item_name": r["item_name"], "warehouse": r["warehouse"], "qty": r["suggested_qty"],
         "unit_cost": r["unit_cost"], "supplier": r["supplier"], "line_total": r["line_total"], "justification": _justification(r)}
        for r in lows
    ]
    fp = _fingerprint(scope, session_id, lines)
    existing = db.sql('SELECT id FROM "Reorder Proposal" WHERE fingerprint = ? AND status = ?', [fp, "proposed"])
    if existing:  # idempotent: an identical open proposal is returned instead of duplicated
        return get_proposal(existing[0]["id"], session_id=session_id if scope == "sandbox" else None, scope=scope, before=state)
    pid = "RP-" + uuid.uuid4().hex[:10]
    summary = " ".join(l["justification"] for l in lines)
    db.sql(
        'INSERT INTO "Reorder Proposal" (id, scope, session_id, created_by, created_at, status, fingerprint, lines_json, justification) VALUES (?,?,?,?,?,?,?,?,?)',
        [pid, scope, session_id, (user or {}).get("email") or (user or {}).get("name"), _now(), "proposed", fp, json.dumps(lines), summary],
    )
    db.conn.commit()
    audit("proposed", actor=(user or {}).get("email"), role=(user or {}).get("role", "anonymous"), session_id=session_id, proposal_id=pid,
          detail={"items": [l["item_code"] for l in lines], "total": round(sum(l["line_total"] for l in lines), 2)})
    return get_proposal(pid, session_id=session_id if scope == "sandbox" else None, scope=scope, before=state)


def get_proposal(pid: str, *, session_id: str | None, scope: str, before: dict | None = None) -> dict:
    db = get_db()
    rows = db.sql('SELECT * FROM "Reorder Proposal" WHERE id = ?', [pid])
    if not rows or rows[0]["scope"] != scope or (scope == "sandbox" and rows[0]["session_id"] != session_id):
        raise NotFound("Proposal not found.")
    r = rows[0]
    lines = json.loads(r["lines_json"])
    return {
        "id": r["id"], "scope": r["scope"], "status": r["status"], "created_at": r["created_at"],
        "lines": lines, "total": round(sum(l["line_total"] for l in lines), 2),
        "justification": r["justification"], "order_ref": r["order_ref"],
        "confirmed_at": r["confirmed_at"], "before": before,
    }


# --- confirm ---------------------------------------------------------------------------


def _claim(db, pid: str) -> dict:
    """Atomically move proposed -> confirming; exactly one caller can win."""
    cur = db.conn.execute('UPDATE "Reorder Proposal" SET status = ? WHERE id = ? AND status = ?', ["confirming", pid, "proposed"])
    db.conn.commit()
    row = db.sql('SELECT * FROM "Reorder Proposal" WHERE id = ?', [pid])
    if not row:
        raise NotFound("Proposal not found.")
    if cur.rowcount != 1:
        raise Conflict(f"Duplicate confirmation: proposal {pid} is already {row[0]['status']}" + (f" ({row[0]['order_ref']})." if row[0]["order_ref"] else "."))
    return row[0]


def _require_explicit(confirm) -> None:
    if confirm is not True:
        raise ReorderError("Explicit confirmation required: send confirm=true after reviewing the proposal.")


def _release(db, pid: str, status: str) -> None:
    db.sql('UPDATE "Reorder Proposal" SET status = ? WHERE id = ?', [status, pid])
    db.conn.commit()


def _still_valid(db, row: dict, session_id: str | None) -> bool:
    """Stale-proposal guard: confirm only if recomputing now gives the same plan."""
    state = low_stock(session_id)
    lines = [{"item_code": r["item_code"], "qty": r["suggested_qty"], "supplier": r["supplier"]} for r in state["items"] if r["is_low"]]
    stored = json.loads(row["lines_json"])
    return _fingerprint(row["scope"], row["session_id"], lines) == _fingerprint(row["scope"], row["session_id"], stored)


def confirm_sandbox(*, session_id: str, proposal_id: str, confirm) -> dict:
    db = get_db()
    _require_explicit(confirm)
    get_proposal(proposal_id, session_id=session_id, scope="sandbox")  # ownership check
    caps = _caps()
    mine = db.sql('SELECT COUNT(*) AS c FROM "Reorder Sandbox Order" WHERE session_id = ?', [session_id])[0]["c"]
    if mine >= caps["per_session"]:
        audit("denied_cap", session_id=session_id, role="anonymous", proposal_id=proposal_id, detail={"cap": "per_session"})
        raise CapExceeded(f"Sandbox order limit ({caps['per_session']}) reached for this session.")
    hour_ago = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - 3600, timezone.utc).isoformat(timespec="seconds")
    recent = db.sql('SELECT COUNT(*) AS c FROM "Reorder Sandbox Order" WHERE created_at > ?', [hour_ago])[0]["c"]
    if recent >= caps["per_hour"]:
        audit("denied_cap", session_id=session_id, role="anonymous", proposal_id=proposal_id, detail={"cap": "global_hour"})
        raise CapExceeded("The public sandbox is busy; try again later.")
    before = low_stock(session_id)
    try:
        row = _claim(db, proposal_id)
    except Conflict:
        audit("denied_duplicate", role="anonymous", session_id=session_id, proposal_id=proposal_id)
        raise
    if not _still_valid(db, row, session_id):
        _release(db, proposal_id, "stale")
        audit("denied_stale", session_id=session_id, role="anonymous", proposal_id=proposal_id)
        raise Conflict("Proposal is stale: stock changed since it was drafted. Create a new proposal.")
    lines = json.loads(row["lines_json"])
    oid = "SBX-" + uuid.uuid4().hex[:8]
    total = round(sum(l["line_total"] for l in lines), 2)
    db.sql('INSERT INTO "Reorder Sandbox Order" (id, session_id, proposal_id, created_at, lines_json, total) VALUES (?,?,?,?,?,?)',
           [oid, session_id, proposal_id, _now(), json.dumps(lines), total])
    db.sql('UPDATE "Reorder Proposal" SET status = ?, order_ref = ?, confirmed_at = ?, confirmed_by = ? WHERE id = ?',
           ["confirmed", oid, _now(), "sandbox-visitor", proposal_id])
    db.conn.commit()
    audit("confirmed_sandbox", role="anonymous", session_id=session_id, proposal_id=proposal_id, detail={"order": oid, "total": total})
    return {"proposal_id": proposal_id, "order_ref": oid, "mode": "sandbox", "total": total, "before": before, "after": low_stock(session_id)}


def confirm_real(*, proposal_id: str, confirm, user: dict) -> dict:
    """Create and submit real Purchase Orders (one per supplier) through the ERP lifecycle."""
    db = get_db()
    _require_explicit(confirm)
    row = db.sql('SELECT * FROM "Reorder Proposal" WHERE id = ?', [proposal_id])
    if not row or row[0]["scope"] != "erp":
        raise NotFound("Proposal not found.")
    before = low_stock()
    try:
        row = _claim(db, proposal_id)
    except Conflict:
        audit("denied_duplicate", actor=user.get("email") or user.get("name"), role=user.get("role"), proposal_id=proposal_id)
        raise

    if not _still_valid(db, row, None):
        _release(db, proposal_id, "stale")
        audit("denied_stale", actor=user.get("email"), role=user.get("role"), proposal_id=proposal_id)
        raise Conflict("Proposal is stale: stock changed since it was drafted. Create a new proposal.")
    lines = json.loads(row["lines_json"])
    from api import services

    company = db.get_all("Company", fields=["name"])[0]["name"]
    refs = []
    try:
        for supplier in sorted({l["supplier"] for l in lines}):
            sup = db.sql('SELECT name FROM "Supplier" WHERE supplier_name = ? OR name = ?', [supplier, supplier])
            if not sup:
                raise ReorderError(f"Supplier '{supplier}' is not set up in the ERP.")
            doc = services.create_document("purchase-order", {
                "supplier": sup[0]["name"], "company": company, "transaction_date": nowdate(), "schedule_date": nowdate(),
                "items": [{"item_code": l["item_code"], "qty": l["qty"], "rate": l["unit_cost"], "warehouse": l["warehouse"]} for l in lines if l["supplier"] == supplier],
            })
            services.submit_document("purchase-order", doc["name"])
            refs.append(doc["name"])
    except Exception:
        _release(db, proposal_id, "proposed")  # nothing is half-confirmed: allow a retry
        raise
    db.sql('UPDATE "Reorder Proposal" SET status = ?, order_ref = ?, confirmed_at = ?, confirmed_by = ? WHERE id = ?',
           ["confirmed", ",".join(refs), _now(), user.get("email") or user.get("name"), proposal_id])
    db.conn.commit()
    audit("confirmed_real", actor=user.get("email") or user.get("name"), role=user.get("role"), proposal_id=proposal_id, detail={"purchase_orders": refs})
    return {"proposal_id": proposal_id, "order_ref": ",".join(refs), "mode": "real", "purchase_orders": refs, "before": before, "after": low_stock()}
