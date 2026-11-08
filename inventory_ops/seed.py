"""Deterministic, original, fictional print-supply dataset (Meridian Print & Pack).

No real companies, customers, prices or payment data. Idempotent: safe on every boot.
"""

from __future__ import annotations

from lambda_erp.accounting.chart_of_accounts import setup_chart_of_accounts, setup_cost_center
from lambda_erp.database import get_db
from lambda_erp.utils import _dict, nowdate

COMPANY = "Meridian Print & Pack"
WAREHOUSE = "Pressroom Stock"

SUPPLIERS = {
    "SUP-TIDE": "Tidewater Paper Mills",
    "SUP-COBALT": "Cobalt Ink Works",
    "SUP-CRATE": "Parcel & Crate Co",
}

# code, name, uom, supplier, unit_cost, on_hand, reorder_level, target_level, pack
ITEMS = [
    ("PAP-100G-A3", "Gloss paper 100gsm A3 (ream)", "Nos", "Tidewater Paper Mills", 7.40, 8, 20, 60, 1),
    ("PAP-200G-A4", "Silk cardstock 200gsm A4 (ream)", "Nos", "Tidewater Paper Mills", 11.90, 35, 15, 40, 1),
    ("INK-CYN-1L", "Cyan UV ink 1L", "Nos", "Cobalt Ink Works", 48.00, 3, 6, 18, 3),
    ("INK-BLK-1L", "Black UV ink 1L", "Nos", "Cobalt Ink Works", 41.50, 14, 6, 18, 3),
    ("INK-MAG-1L", "Magenta UV ink 1L", "Nos", "Cobalt Ink Works", 48.00, 2, 6, 18, 3),
    ("PKG-MAIL-M", "Mailer box M (flat pack)", "Nos", "Parcel & Crate Co", 0.62, 140, 200, 800, 50),
    ("PKG-TUBE-A2", "Poster tube A2", "Nos", "Parcel & Crate Co", 1.35, 90, 60, 240, 20),
    ("LAM-FILM-GL", "Gloss lamination film roll", "Nos", "Tidewater Paper Mills", 29.00, 4, 5, 15, 1),
]

# One open PO already covers magenta ink, so it is NOT flagged: demonstrates on-order logic.
OPEN_PO = {"supplier": "Cobalt Ink Works", "item": "INK-MAG-1L", "qty": 6}


def seed() -> None:
    from lambda_erp.buying.purchase_order import PurchaseOrder
    from lambda_erp.stock.stock_entry import StockEntry

    db = get_db()
    if not db.get_all("Company", fields=["name"]):
        db.insert("Company", _dict(name=COMPANY, company_name=COMPANY, default_currency="USD"))
        setup_chart_of_accounts(COMPANY, "USD")
        setup_cost_center(COMPANY)
    if db.exists("Warehouse", f"{WAREHOUSE} - MPP"):
        return  # already seeded
    wh = f"{WAREHOUSE} - MPP"
    db.insert("Warehouse", _dict(name=wh, warehouse_name=WAREHOUSE, company=COMPANY))
    for code, sname in SUPPLIERS.items():
        db.insert("Supplier", _dict(name=code, supplier_name=sname))
    for code, name, uom, _sup, cost, *_ in ITEMS:
        db.insert("Item", _dict(name=code, item_name=name, stock_uom=uom, standard_rate=cost, is_stock_item=1))
    receipt = StockEntry(
        stock_entry_type="Material Receipt", posting_date=nowdate(), company=COMPANY,
        items=[_dict(item_code=c, qty=oh, basic_rate=cost, t_warehouse=wh) for c, _n, _u, _s, cost, oh, *_ in ITEMS],
    )
    receipt.save()
    receipt.submit()
    for code, _n, _u, sup, cost, _oh, level, target, pack in ITEMS:
        db.sql(
            'INSERT INTO "Reorder Policy" (item_code, warehouse, reorder_level, target_level, pack_size, supplier, unit_cost) VALUES (?,?,?,?,?,?,?)',
            [code, wh, level, target, pack, sup, cost],
        )
    db.conn.commit()
    sup_id = next(k for k, v in SUPPLIERS.items() if v == OPEN_PO["supplier"])
    po = PurchaseOrder(
        supplier=sup_id, company=COMPANY, transaction_date=nowdate(), schedule_date=nowdate(),
        items=[_dict(item_code=OPEN_PO["item"], qty=OPEN_PO["qty"], rate=48.0, warehouse=wh)],
    )
    po.save()
    po.submit()
