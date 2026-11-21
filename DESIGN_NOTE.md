# Design Note: Order-to-Audit System

**Author:** Maimathi U | **Target:** CDI, NIT Tiruchirappalli (Winter 2026)

## 1. Architecture & Data Model
The system links the **Sales Loop** and **Production Loop** around a single immutable **Stock Ledger** (`stock_ledger`) in SQLite. Stock balances are never stored as mutable counters; every balance is derived as `SUM(qty_delta)` over `stock_ledger`, where every row references its `source_type` (`OPENING`, `PURCHASE`, `PRODUCTION_RUN`, `INVOICE`, `CREDIT_RETURN`) and `source_id`.

### Core Entities & Relationships
- **Master Data:** `users` (5 RBAC roles), `items` (raw_material, packaging, finished_good), `customers` (state for CGST+SGST vs IGST), `suppliers`, `boms` + `bom_lines` (recipe per batch & tolerance %).
- **Production Loop:** `purchases` → `stock_ledger` (+IN) → `production_runs` + `production_consumptions` → `stock_ledger` (-OUT raw/packaging/damaged, +IN finished goods).
- **Sales Loop:** `sales_orders` + `sales_order_lines` → `invoices` (FY numbering, CGST/SGST or IGST, credit-note cancellation) → `stock_ledger` (-OUT dispatch) → `payments`.
- **Auditability:** `audit_logs` records `(user_id, action, entity_type, entity_id, details, created_at)` for every state change. Posted invoices are never deleted; they transition to `CANCELLED_BY_CREDIT_NOTE`.

## 2. Stock & BOM Computation
- **Unit Normalization:** All quantities are converted to each item's `base_unit` (`kg ↔ g`, `litre ↔ ml`, `carton ↔ packet/piece`) using `decimal.Decimal` before ledger insertion.
- **Planned vs. Actual & Variance Flagging:** On starting a run, `planned_qty = qty_per_batch × batches`. Variance `% = ((actual - planned) / planned) × 100`. Any material variance or output yield variance exceeding `±tolerance_pct` (3%) sets `flagged = 1`.
- **Damaged Packaging:** Good pouches used (`188`) are recorded as `PRODUCTION_ISSUE` and damaged pouches (`6`) as `DAMAGE_WASTAGE` in `stock_ledger`, both included in run material cost.

## 3. Costing & Valuation Method
- **Money Precision:** All monetary values are stored as integer **paisa** (`1 INR = 100 paisa`) using `ROUND_HALF_UP`, eliminating floating-point drift.
- **Inventory Valuation (Weighted Average Cost):** For each item, `WAC = SUM(total_cost_paisa) / SUM(qty_delta)` across inward movements (`OPENING`, `PURCHASE`, `PRODUCTION_OUTPUT`). Example: `100 kg @ ₹42 + 60 kg @ ₹45` yields `₹43.125/kg`.
- **Overhead Allocation:** Direct per-run allocation (`labour_cost_paisa + overhead_cost_paisa`) added to `material_cost_paisa`, divided by `actual_output_packets` (`₹5,855.00 / 188 = ₹31.14/packet`).
