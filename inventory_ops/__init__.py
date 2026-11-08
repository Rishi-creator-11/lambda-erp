"""Inventory Ops Copilot: approval-gated reorder workflow for Lambda ERP.

Added by Rishi Kumar as a deployment plugin (LAMBDA_ERP_PLUGINS=inventory_ops); it
uses Lambda ERP's own extension seams (register_table, register_action, register_hook)
and does not edit core business logic. Apache-2.0, like the upstream project.
"""

from .plugin import register  # noqa: F401
