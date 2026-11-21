import sqlite3
from pathlib import Path
from typing import Optional

from app.db import DEFAULT_DB_PATH, get_connection, init_db, to_paisa
from app.services import hash_password, now_iso, today_iso


def reset_and_seed(db_path: Optional[Path | str] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    if path.exists():
        path.unlink()
    conn = get_connection(path)
    init_db(conn)
    ts = now_iso()
    today = today_iso()

    # 1. Users (all 5 roles + Salesman B for benchmark)
    users = [
        ("salesman_a", "Salesman A (Arjun)", "salesman", hash_password("demo123")),
        ("salesman_b", "Salesman B (Karthik)", "salesman", hash_password("demo123")),
        ("admin", "Factory Admin (Priya)", "admin", hash_password("demo123")),
        ("production", "Production Supervisor (Senthil)", "production", hash_password("demo123")),
        ("auditor", "External Auditor (Ramesh CA)", "auditor", hash_password("demo123")),
        ("owner", "Factory Owner (Lakshmi)", "owner", hash_password("demo123")),
    ]
    conn.executemany(
        "INSERT INTO users (username, full_name, role, password_hash) VALUES (?, ?, ?, ?)",
        users,
    )

    # 2. Items (Section 5 Master Data)
    items = [
        # sku, name, category, base_unit, hsn_code, gst_rate_bps, selling_price_paisa, reorder_level, pack_size_g, packets_per_carton
        ("RM-FLOUR", "Rice & Besan Flour", "raw_material", "kg", "11029090", 500, 0, "50", None, 20),
        ("RM-OIL", "Cooking Oil", "raw_material", "litre", "15119090", 500, 0, "15", None, 20),
        ("RM-SPICE", "Salt & Spice Mix", "raw_material", "kg", "09109100", 500, 0, "2", None, 20),
        ("PK-POUCH200", "200g Pouch", "packaging", "piece", "39232100", 1800, 0, "200", None, 20),
        ("PK-CARTON", "Master Carton (20 pkts)", "packaging", "piece", "48191010", 1800, 0, "15", None, 20),
        ("FG-MURUKKU200", "Murukku 200 g", "finished_good", "packet", "21069099", 500, to_paisa(50), "100", 200, 20),
    ]
    conn.executemany(
        """
        INSERT INTO items (sku, name, category, base_unit, hsn_code, gst_rate_bps, selling_price_paisa, reorder_level, pack_size_g, packets_per_carton)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        items,
    )

    item_map = {r["sku"]: r["id"] for r in conn.execute("SELECT id, sku FROM items").fetchall()}

    # 3. Customers & Suppliers
    conn.executemany(
        "INSERT INTO customers (name, customer_type, state, gstin, phone, address) VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("Retailer A", "retailer", "Tamil Nadu", "33AABCR1234A1Z5", "9876543210", "RS Puram, Coimbatore, TN"),
            ("Distributor B", "distributor", "Karnataka", "29AABCD5678B1Z2", "9845012345", "Jayanagar, Bengaluru, KA"),
        ],
    )
    conn.execute(
        "INSERT INTO suppliers (name, state, gstin, phone) VALUES (?, ?, ?, ?)",
        ("Sri Annapoorna Flour Mills", "Tamil Nadu", "33AABCS9999K1Z1", "9443011223"),
    )

    # 4. BOM for Murukku 200 g (1 batch = 200 packets expected, 3% tolerance)
    cur = conn.execute(
        "INSERT INTO boms (finished_item_id, name, expected_yield_packets, tolerance_pct) VALUES (?, ?, ?, ?)",
        (item_map["FG-MURUKKU200"], "Murukku 200 g Standard Batch", 200, "3"),
    )
    bom_id = cur.lastrowid
    bom_lines = [
        (bom_id, item_map["RM-FLOUR"], "40", "kg"),
        (bom_id, item_map["RM-OIL"], "10", "litre"),
        (bom_id, item_map["RM-SPICE"], "1.2", "kg"),
        (bom_id, item_map["PK-POUCH200"], "200", "piece"),
        (bom_id, item_map["PK-CARTON"], "10", "piece"),
    ]
    conn.executemany(
        "INSERT INTO bom_lines (bom_id, item_id, qty_per_batch, unit) VALUES (?, ?, ?, ?)",
        bom_lines,
    )

    # 5. Opening Stock in stock_ledger
    opening_entries = [
        (item_map["RM-FLOUR"], "100", "kg", to_paisa("42"), 100 * to_paisa("42"), "OPEN-FLOUR-01"),
        (item_map["RM-OIL"], "30", "litre", to_paisa("140"), 30 * to_paisa("140"), "OPEN-OIL-01"),
        (item_map["RM-SPICE"], "5", "kg", to_paisa("200"), 5 * to_paisa("200"), "OPEN-SPICE-01"),
        (item_map["PK-POUCH200"], "500", "piece", to_paisa("2.50"), 500 * to_paisa("2.50"), "OPEN-POUCH-01"),
        (item_map["PK-CARTON"], "30", "piece", to_paisa("15"), 30 * to_paisa("15"), "OPEN-CARTON-01"),
        (item_map["FG-MURUKKU200"], "50", "packet", to_paisa("30"), 50 * to_paisa("30"), "OPEN-FG-01"),
    ]
    for item_id, qty, unit, uc, tc, batch in opening_entries:
        conn.execute(
            """
            INSERT INTO stock_ledger (
                item_id, txn_date, movement_type, qty_delta, unit, unit_cost_paisa, total_cost_paisa,
                source_type, source_id, batch_no, notes, created_by, created_at
            ) VALUES (?, ?, 'OPENING', ?, ?, ?, ?, 'OPENING', 0, ?, 'Section 5 Opening Stock', 3, ?)
            """,
            (item_id, today, qty, unit, uc, tc, batch, ts),
        )

    conn.commit()
    return conn


if __name__ == "__main__":
    c = reset_and_seed()
    print(f"Seeded Section 5 master data and opening stock at {DEFAULT_DB_PATH}")
    c.close()
