"""Wires the plugin into Lambda ERP's registries (called once at boot)."""

from api import services
from lambda_erp.hooks import register_hook

from . import service

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS "Reorder Policy" (
        item_code TEXT PRIMARY KEY,
        warehouse TEXT NOT NULL,
        reorder_level REAL NOT NULL,
        target_level REAL NOT NULL,
        pack_size REAL DEFAULT 1,
        supplier TEXT NOT NULL,
        unit_cost REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS "Reorder Proposal" (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        session_id TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        lines_json TEXT NOT NULL,
        justification TEXT,
        order_ref TEXT,
        confirmed_at TEXT,
        confirmed_by TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS "Reorder Sandbox Order" (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        proposal_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        lines_json TEXT NOT NULL,
        total REAL
    )""",
    """CREATE TABLE IF NOT EXISTS "Reorder Audit Event" (
        id TEXT PRIMARY KEY,
        ts TEXT NOT NULL,
        actor TEXT,
        role TEXT,
        session_id TEXT,
        event TEXT NOT NULL,
        proposal_id TEXT,
        detail TEXT
    )""",
]


def _propose(args: dict, user: dict):
    return service.propose(user=user, session_id=None, scope="erp")


def _confirm(args: dict, user: dict):
    return service.confirm_real(
        proposal_id=str(args.get("proposal_id", "")),
        confirm=args.get("confirm"),
        user=user,
    )


def register() -> None:
    for ddl in SCHEMA:
        services.register_table(ddl)

    # Read-only: any signed-in role may look at low stock and request a draft proposal
    # (a proposal is a plan, not an ERP write).
    services.register_action(
        "list_low_stock",
        lambda args: service.low_stock(),
        description="List stock items at or below their reorder level, counting open purchase orders.",
        minimum_role="viewer",
    )
    services.register_action(
        "propose_reorder",
        _propose,
        description="Create a draft reorder proposal (quantities + justification) for low-stock items. Writes nothing to purchasing.",
        minimum_role="viewer",
        pass_user=True,
    )
    # The write: upstream role gate = manager+, shared demo account excluded (default).
    services.register_action(
        "confirm_reorder",
        _confirm,
        description=(
            "Confirm a reorder proposal and create real Purchase Orders. Requires confirm=true; "
            "a proposal can be confirmed once."
        ),
        parameters={
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "confirm": {"type": "boolean", "description": "Must be true: explicit human confirmation."},
            },
            "required": ["proposal_id", "confirm"],
        },
        minimum_role="manager",
        pass_user=True,
    )

    # Existing mutation hook: every Purchase Order submit (from any path) leaves an audit event.
    register_hook("Purchase Order:after_submit", service.audit_po_submit)
