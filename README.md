# Order-to-Audit System for a Small Food Manufacturer

**Centre for Digital Infrastructure (CDI), NIT Tiruchirappalli — Software Developer Intern (Winter 2026)**

A ledger-based, role-aware Order-to-Audit web application linking the **Sales Loop** (mobile field order → admin verification → GST invoice → dispatch → collections) and the **Production Loop** (purchase → raw/packaging stock → BOM-driven production run → planned-vs-actual variance → finished goods → costing & audit reports).

---

## 1. Tech Stack & Why
- **Backend:** Python 3.11 + **FastAPI** + **SQLite** (`sqlite3`). Zero external database daemon is needed, making single-command local execution and evaluation effortless while enforcing foreign keys, transactions, and a strict movement ledger.
- **Arithmetic:** Integer **paisa** (`int`) for all currency and `decimal.Decimal` for all quantities and unit conversions (`kg ↔ g`, `litre ↔ ml`, `carton ↔ packet/piece`), preventing floating-point rounding drift.
- **Exports & UI:** **ReportLab** (GST Tax Invoice PDF & Monthly Audit PDF), **OpenPyXL** (`.xlsx` multi-sheet workbook), standard-library `csv`, and a responsive **HTML/JS SPA** with a mobile-first Salesman order screen (including `localStorage` offline order queuing) and desktop views for Admin, Production, Auditor, and Owner.

---

## 2. Quick Start (Single Command)

### Option A: Local Python
```bash
pip install -r requirements.txt
python run.py
```
Open **http://127.0.0.1:8000** in your browser. On first launch, the app automatically seeds Section 5 master data and opening stock.

### Option B: Docker Compose
```bash
docker compose up --build
```

### Run Automated Benchmark & Unit Tests
```bash
python -m unittest discover -s tests -v
```

### Reset Database to Section 5 Opening State
```bash
python seed_benchmark.py
```
*(Or click **"Reset to Section 5 Opening State"** in the Factory Admin view.)*

---

## 3. Test Logins (Role-Based Access)
Password for all demo accounts: **`demo123`** (or use the 1-click role buttons on the login screen):

| Role | Username | Access Scope |
|---|---|---|
| Salesman A (Mobile) | `salesman_a` | Quick mobile order entry, offline queue & sync, own orders |
| Salesman B (Mobile) | `salesman_b` | Quick mobile order entry, own orders |
| Production Supervisor | `production` | Raw/packaging purchases, BOM production runs, variance & overheads |
| Factory Admin | `admin` | Order verification, stock checks, GST invoicing (PDF), dispatch, payments |
| Accountant / Auditor | `auditor` | Read-only monthly reports (GSTR-1/3B, registers, ledger), Excel/CSV/PDF export, audit trail |
| Owner | `owner` | Executive KPIs, alerts, full visibility |

---

## 4. Feature Coverage vs. Section 6 Scope

- **Must Have (100% Built):** Role-based login (5 roles), Product/Customer/Raw-Material/BOM masters, mobile-friendly order entry & admin approval, GST invoice PDF (CGST+SGST vs IGST, FY numbering), BOM production run updating raw & finished stock automatically, planned vs actual consumption & yield with ±3% variance flagging, ledger-based stock summary & stock ledger, Excel/CSV/PDF exports.
- **Should Have (100% Built):** Purchases & supplier records, payments & customer receivables with ageing, cost per packet with labour/gas overheads, GST summary (GSTR-1 / GSTR-3B shape), variance alerts beyond tolerance, Owner dashboard.
- **Stretch Goals Built:** Offline order capture with `localStorage` queue & idempotent sync (`client_offline_id`), batch/expiry fields on purchases and stock ledger, low-stock reorder level alerts.

---

## 5. Section 5 Benchmark Verification Results

All 9 steps are verified automatically by `tests/test_benchmark.py`:

| Check | Expected | System Output | Notes |
|---|---|---|---|
| Flour closing balance | `120 kg` | **120 kg** | `100 + 60 − 40` via `stock_ledger` |
| Oil closing balance | `19.5 L` | **19.5 L** | `30 − 10.5` |
| Pouches closing balance | `306` | **306** | `500 − 188 (used) − 6 (damaged)` |
| Finished goods after run | `238 packets` | **238 packets** | `50 + 188` |
| Finished goods after Retailer A dispatch | `88 packets` | **88 packets** | `238 − 150` |
| Yield variance | `188 vs 200, -6%, flagged` | **-6.00%, FLAGGED** | Exceeds ±3% BOM tolerance |
| Oil consumption variance | `+5%, flagged` | **+5.00%, FLAGGED** | `10.5 L vs 10.0 L` (+5% > 3%) |
| Retailer A invoice | `₹7,500 + ₹187.50 + ₹187.50 = ₹7,875` | **₹7,875.00** | Intra-state TN (CGST 2.5% + SGST 2.5%) |
| Retailer A outstanding | `₹3,875` | **₹3,875.00** | `₹7,875 − ₹4,000` payment |
| Distributor B order | `Shortage of 32 packets flagged; IGST` | **Shortage 32 flagged; IGST** | `88 available vs 120 requested`; approval blocked |
| Cost per packet | Stated method & overhead rule | **₹31.14 / packet** | Weighted Average Cost + direct run overheads (see below) |

### Costing & Overhead Explanation
- **Inventory Valuation:** **Weighted Average Cost (WAC)**. After purchasing 60 kg flour at ₹45/kg on top of 100 kg opening stock at ₹42/kg, flour WAC is `(100×42 + 60×45) / 160 = ₹43.125/kg`.
- **Direct Material & Packaging Cost for Run:** `40×43.125 (₹1,725) + 10.5×140 (₹1,470) + 1.2×200 (₹240) + 194×2.50 (₹485) + 9×15 (₹135) = ₹4,055.00`.
- **Overhead Allocation:** Direct per-run allocation of Labour (`₹1,200`) + Gas/Power (`₹600`) = `₹1,800.00`.
- **Total Run Cost:** `₹4,055.00 + ₹1,800.00 = ₹5,855.00` across `188` good packets = **`₹31.14` per packet** (gross margin `₹18.86` or `37.7%` against `₹50.00` selling price).

---

## 6. Assumptions & Known Gaps
- **Assumptions:** All 5% GST items use HSN `21069099` (Murukku); Factory is located in Tamil Nadu (State Code `33`); Credit Note cancellation reverses dispatched stock into `stock_ledger` without deleting the original invoice.
- **Known Gaps:** In-memory session dictionary is used for demo auth (re-login required on server restart); Tally XML export is omitted in favor of Excel/CSV/PDF exports.
