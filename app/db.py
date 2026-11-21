import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "order_to_audit.db"


def to_paisa(rupees: Decimal | str | int | float) -> int:
    """Convert rupee amount to integer paisa using Decimal (never float arithmetic)."""
    d = Decimal(str(rupees))
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_paisa(paisa: int) -> str:
    """Format integer paisa as rupee string with 2 decimal places."""
    d = Decimal(paisa) / Decimal(100)
    return f"{d:.2f}"


def to_qty(val: Decimal | str | int | float) -> str:
    """Normalize quantity to a clean Decimal string (up to 4 decimal places)."""
    d = Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    s = format(d.normalize(), "f")
    return s if "." not in s else s.rstrip("0").rstrip(".")


def dec(val: Decimal | str | int | float) -> Decimal:
    return Decimal(str(val))


def get_connection(db_path: Optional[Path | str] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('salesman', 'admin', 'production', 'auditor', 'owner')),
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('raw_material', 'packaging', 'wip', 'finished_good')),
    base_unit TEXT NOT NULL,
    hsn_code TEXT NOT NULL DEFAULT '21069099',
    gst_rate_bps INTEGER NOT NULL DEFAULT 500, -- 500 bps = 5.00%
    selling_price_paisa INTEGER NOT NULL DEFAULT 0,
    reorder_level TEXT NOT NULL DEFAULT '0',
    pack_size_g INTEGER DEFAULT NULL,
    packets_per_carton INTEGER DEFAULT 20
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    customer_type TEXT NOT NULL CHECK(customer_type IN ('retailer', 'distributor')),
    state TEXT NOT NULL,
    gstin TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    address TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    gstin TEXT DEFAULT '',
    phone TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS boms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finished_item_id INTEGER NOT NULL REFERENCES items(id),
    name TEXT NOT NULL,
    expected_yield_packets INTEGER NOT NULL,
    tolerance_pct TEXT NOT NULL DEFAULT '3',
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS bom_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bom_id INTEGER NOT NULL REFERENCES boms(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty_per_batch TEXT NOT NULL,
    unit TEXT NOT NULL
);

-- Ledger-based inventory: stock balance is ALWAYS SUM(qty_delta) per item
CREATE TABLE IF NOT EXISTS stock_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id),
    txn_date TEXT NOT NULL,
    movement_type TEXT NOT NULL CHECK(movement_type IN (
        'OPENING', 'PURCHASE', 'PRODUCTION_ISSUE', 'PRODUCTION_OUTPUT',
        'DAMAGE_WASTAGE', 'SALES_DISPATCH', 'ADJUSTMENT', 'CREDIT_RETURN'
    )),
    qty_delta TEXT NOT NULL, -- Decimal string (+ for IN, - for OUT)
    unit TEXT NOT NULL,
    unit_cost_paisa INTEGER NOT NULL DEFAULT 0,
    total_cost_paisa INTEGER NOT NULL DEFAULT 0,
    source_type TEXT NOT NULL, -- 'OPENING', 'PURCHASE', 'PRODUCTION_RUN', 'INVOICE', 'ADJUSTMENT'
    source_id INTEGER NOT NULL DEFAULT 0,
    batch_no TEXT DEFAULT '',
    expiry_date TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER REFERENCES suppliers(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    purchase_date TEXT NOT NULL,
    qty TEXT NOT NULL,
    unit TEXT NOT NULL,
    rate_paisa INTEGER NOT NULL,
    total_paisa INTEGER NOT NULL,
    invoice_no TEXT DEFAULT '',
    batch_no TEXT DEFAULT '',
    expiry_date TEXT DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS production_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_no TEXT UNIQUE NOT NULL,
    bom_id INTEGER NOT NULL REFERENCES boms(id),
    run_date TEXT NOT NULL,
    batches TEXT NOT NULL DEFAULT '1',
    expected_output_packets INTEGER NOT NULL,
    actual_output_packets INTEGER DEFAULT 0,
    yield_variance_pct TEXT DEFAULT '0',
    yield_flagged INTEGER NOT NULL DEFAULT 0,
    labour_cost_paisa INTEGER NOT NULL DEFAULT 0,
    overhead_cost_paisa INTEGER NOT NULL DEFAULT 0,
    material_cost_paisa INTEGER NOT NULL DEFAULT 0,
    total_cost_paisa INTEGER NOT NULL DEFAULT 0,
    cost_per_packet_paisa INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'IN_PROGRESS' CHECK(status IN ('IN_PROGRESS', 'COMPLETED')),
    notes TEXT DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS production_consumptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES production_runs(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    planned_qty TEXT NOT NULL,
    actual_qty TEXT NOT NULL,
    damaged_qty TEXT NOT NULL DEFAULT '0',
    variance_pct TEXT NOT NULL DEFAULT '0',
    flagged INTEGER NOT NULL DEFAULT 0,
    unit TEXT NOT NULL,
    cost_paisa INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sales_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT UNIQUE NOT NULL,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    salesman_id INTEGER NOT NULL REFERENCES users(id),
    order_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'SUBMITTED' CHECK(status IN (
        'SUBMITTED', 'APPROVED', 'REJECTED', 'INVOICED', 'DISPATCHED'
    )),
    stock_shortage_flagged INTEGER NOT NULL DEFAULT 0,
    shortage_details TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    client_offline_id TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales_order_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty INTEGER NOT NULL,
    unit_price_paisa INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no TEXT UNIQUE NOT NULL,
    order_id INTEGER NOT NULL REFERENCES sales_orders(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    invoice_date TEXT NOT NULL,
    financial_year TEXT NOT NULL,
    place_of_supply TEXT NOT NULL,
    is_interstate INTEGER NOT NULL DEFAULT 0,
    taxable_paisa INTEGER NOT NULL,
    cgst_paisa INTEGER NOT NULL DEFAULT 0,
    sgst_paisa INTEGER NOT NULL DEFAULT 0,
    igst_paisa INTEGER NOT NULL DEFAULT 0,
    total_paisa INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'POSTED' CHECK(status IN ('POSTED', 'DISPATCHED', 'CANCELLED_BY_CREDIT_NOTE')),
    credit_note_no TEXT DEFAULT '',
    dispatched_at TEXT DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    invoice_id INTEGER REFERENCES invoices(id),
    payment_date TEXT NOT NULL,
    amount_paisa INTEGER NOT NULL,
    mode TEXT NOT NULL DEFAULT 'UPI',
    reference_no TEXT DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()
