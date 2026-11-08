# Modifications to Lambda ERP (Apache-2.0 section 4(b))

Based on lambdadevelopment/lambda-erp @ d12e22063970c22801ac81f5457f5e09c80d8c93. Changes by Rishi Kumar, 2026-11:

- Added: `inventory_ops/` (plugin, service, router, seed), `tests/test_inventory_ops.py`, `frontend/src/pages/inventory-ops.tsx`, this file.
- Modified: `api/main.py` (mount the plugin router; optional deterministic seed), `frontend/src/routes.tsx` (public `/inventory-ops` route), `Dockerfile` (plugin env defaults, copy plugin), `README.md` (attribution + change list).
