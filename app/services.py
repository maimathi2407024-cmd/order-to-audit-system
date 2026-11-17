from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import sqlite3
from typing import Any, Dict, List, Optional

from app.db import dec, from_paisa, to_paisa, to_qty

FACTORY_STATE = "Tamil Nadu"
FACTORY_GSTIN = "33AABCU9603R1ZM"

UNIT_CONVERSIONS = {
    ("g", "kg"): Decimal("0.001"),
    ("kg", "g"): Decimal("1000"),
    ("ml", "litre"): Decimal("0.001"),
    ("litre", "ml"): Decimal("1000"),
}


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_iso() -> str:
    return date.today().strftime("%Y-%m-%d")


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def get_financial_year(dt_str: str) -> str:
    dt = datetime.strptime(dt_str[:10], "%Y-%m-%d").date()
    start_year = dt.year if dt.month >= 4 else dt.year - 1
    end_year = (start_year + 1) % 100
    return f"FY{start_year % 100:02d}-{end_year:02d}"


def convert_to_base_unit(
    qty: Decimal, from_unit: str, base_unit: str, packets_per_carton: int = 20
) -> Decimal:
    fu = from_unit.strip().lower()
    bu = base_unit.strip().lower()
    if fu == bu:
        return qty
    if (fu, bu) in UNIT_CONVERSIONS:
        return qty * UNIT_CONVERSIONS[(fu, bu)]
    if fu == "carton" and bu in ("packet", "piece"):
        return qty * Decimal(packets_per_carton)
    if fu in ("packet", "piece") and bu == "carton":
        return qty / Decimal(packets_per_carton)
    raise ValueError(f"Unsupported unit conversion: {from_unit} -> {base_unit}")


def log_audit(
    conn: sqlite3.Connection,
    user_id: Optional[int],
    action: str,
    entity_type: str,
    entity_id: int,
    details: str,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs (user_id, action, entity_type, entity_id, details, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, action, entity_type, entity_id, details, now_iso()),
    )


def get_stock_balance(conn: sqlite3.Connection, item_id: int) -> Decimal:
    rows = conn.execute(
        "SELECT qty_delta FROM stock_ledger WHERE item_id = ?", (item_id,)
    ).fetchall()
    total = Decimal("0")
    for r in rows:
        total += Decimal(r["qty_delta"])
    return total


def get_weighted_avg_cost_paisa(conn: sqlite3.Connection, item_id: int) -> Decimal:
    """
    Compute Weighted Average Cost (in paisa per base unit) from all positive
    inward stock movements (OPENING, PURCHASE, PRODUCTION_OUTPUT, CREDIT_RETURN).
    Returns Decimal paisa (can be fractional paisa, e.g., 4312.5 paisa/kg for ₹43.125/kg).
    """
    rows = conn.execute(
        """
        SELECT qty_delta, total_cost_paisa
        FROM stock_ledger
        WHERE item_id = ? AND movement_type IN ('OPENING', 'PURCHASE', 'PRODUCTION_OUTPUT')
        """,
        (item_id,),
    ).fetchall()
    total_qty = Decimal("0")
    total_cost = Decimal("0")
    for r in rows:
        q = Decimal(r["qty_delta"])
        if q > 0:
            total_qty += q
            total_cost += Decimal(r["total_cost_paisa"])
    if total_qty <= 0:
        return Decimal("0")
    return total_cost / total_qty


def get_all_stock_summary(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    items = conn.execute("SELECT * FROM items ORDER BY category, id").fetchall()
    summary = []
    for item in items:
        bal = get_stock_balance(conn, item["id"])
        wac = get_weighted_avg_cost_paisa(conn, item["id"])
        reorder = Decimal(item["reorder_level"])
        summary.append(
            {
                "id": item["id"],
                "sku": item["sku"],
                "name": item["name"],
                "category": item["category"],
                "base_unit": item["base_unit"],
                "balance": to_qty(bal),
                "wac_rupees": f"{(wac / Decimal(100)):.4f}".rstrip("0").rstrip(".")
                if "." in f"{(wac / Decimal(100)):.4f}"
                else f"{(wac / Decimal(100)):.2f}",
                "wac_paisa": str(wac.quantize(Decimal("0.01"))),
                "stock_value_rupees": from_paisa(
                    int((bal * wac).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                ),
                "reorder_level": item["reorder_level"],
                "low_stock": bal <= reorder and reorder > 0,
                "selling_price_rupees": from_paisa(item["selling_price_paisa"]),
            }
        )
    return summary


def record_purchase(
    conn: sqlite3.Connection,
    item_id: int,
    qty: Decimal | str,
    unit: str,
    rate_rupees: Decimal | str,
    supplier_id: Optional[int] = None,
    purchase_date: Optional[str] = None,
    invoice_no: str = "",
    batch_no: str = "",
    expiry_date: str = "",
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        raise ValueError("Item not found")
    p_date = purchase_date or today_iso()
    qty_dec = convert_to_base_unit(
        dec(qty), unit, item["base_unit"], item["packets_per_carton"] or 20
    )
    rate_p = to_paisa(rate_rupees)
    total_p = int((qty_dec * Decimal(rate_p)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    ts = now_iso()

    cur = conn.execute(
        """
        INSERT INTO purchases (
            supplier_id, item_id, purchase_date, qty, unit, rate_paisa, total_paisa,
            invoice_no, batch_no, expiry_date, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            supplier_id,
            item_id,
            p_date,
            to_qty(qty_dec),
            item["base_unit"],
            rate_p,
            total_p,
            invoice_no,
            batch_no,
            expiry_date,
            user_id,
            ts,
        ),
    )
    purchase_id = cur.lastrowid

    conn.execute(
        """
        INSERT INTO stock_ledger (
            item_id, txn_date, movement_type, qty_delta, unit, unit_cost_paisa, total_cost_paisa,
            source_type, source_id, batch_no, expiry_date, notes, created_by, created_at
        ) VALUES (?, ?, 'PURCHASE', ?, ?, ?, ?, 'PURCHASE', ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            p_date,
            to_qty(qty_dec),
            item["base_unit"],
            rate_p,
            total_p,
            purchase_id,
            batch_no,
            expiry_date,
            f"Purchase #{purchase_id} ({invoice_no or 'No Inv'})",
            user_id,
            ts,
        ),
    )
    log_audit(
        conn,
        user_id,
        "CREATE_PURCHASE",
        "PURCHASE",
        purchase_id,
        f"Purchased {to_qty(qty_dec)} {item['base_unit']} of {item['name']} @ ₹{from_paisa(rate_p)}",
    )
    conn.commit()
    return {
        "purchase_id": purchase_id,
        "item_name": item["name"],
        "qty": to_qty(qty_dec),
        "unit": item["base_unit"],
        "total_rupees": from_paisa(total_p),
        "new_balance": to_qty(get_stock_balance(conn, item_id)),
    }


def start_production_run(
    conn: sqlite3.Connection,
    bom_id: int,
    batches: Decimal | str = "1",
    actual_issues: Optional[Dict[int, Dict[str, Any]]] = None,
    run_date: Optional[str] = None,
    notes: str = "",
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Start a production run from a BOM.
    `actual_issues`: optional map {item_id: {"qty": ..., "unit": ...}} for raw materials issued at start.
    Packaging items (pouch, carton) can be issued at start or at completion.
    """
    bom = conn.execute("SELECT * FROM boms WHERE id = ?", (bom_id,)).fetchone()
    if not bom:
        raise ValueError("BOM not found")
    lines = conn.execute(
        """
        SELECT bl.*, i.name AS item_name, i.category, i.base_unit, i.packets_per_carton
        FROM bom_lines bl
        JOIN items i ON i.id = bl.item_id
        WHERE bl.bom_id = ?
        """,
        (bom_id,),
    ).fetchall()

    b_dec = dec(batches)
    tolerance = dec(bom["tolerance_pct"])
    expected_out = int((Decimal(bom["expected_yield_packets"]) * b_dec).quantize(Decimal("1")))
    r_date = run_date or today_iso()
    ts = now_iso()

    count = conn.execute("SELECT COUNT(*) FROM production_runs").fetchone()[0] + 1
    run_no = f"RUN-{r_date.replace('-', '')}-{count:03d}"

    cur = conn.execute(
        """
        INSERT INTO production_runs (
            run_no, bom_id, run_date, batches, expected_output_packets, status, notes, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, 'IN_PROGRESS', ?, ?, ?)
        """,
        (run_no, bom_id, r_date, to_qty(b_dec), expected_out, notes, user_id, ts),
    )
    run_id = cur.lastrowid

    material_cost_total = 0
    consumptions_out = []
    actual_issues = actual_issues or {}

    for line in lines:
        item_id = line["item_id"]
        planned_base = convert_to_base_unit(
            dec(line["qty_per_batch"]) * b_dec,
            line["unit"],
            line["base_unit"],
            line["packets_per_carton"] or 20,
        )

        # If raw_material, issue now (default to planned if not specified)
        # If packaging, issue only if explicitly provided in actual_issues; otherwise issued on completion
        if item_id in actual_issues:
            iss = actual_issues[item_id]
            actual_base = convert_to_base_unit(
                dec(iss["qty"]),
                iss.get("unit", line["base_unit"]),
                line["base_unit"],
                line["packets_per_carton"] or 20,
            )
        elif line["category"] == "raw_material":
            actual_base = planned_base
        else:
            actual_base = Decimal("0")

        var_pct = Decimal("0")
        flagged = 0
        if actual_base > 0 and planned_base > 0:
            var_pct = ((actual_base - planned_base) / planned_base) * Decimal("100")
            if abs(var_pct) > tolerance:
                flagged = 1

        wac_paisa = get_weighted_avg_cost_paisa(conn, item_id)
        cost_p = int((actual_base * wac_paisa).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        material_cost_total += cost_p

        conn.execute(
            """
            INSERT INTO production_consumptions (
                run_id, item_id, planned_qty, actual_qty, damaged_qty, variance_pct, flagged, unit, cost_paisa
            ) VALUES (?, ?, ?, ?, '0', ?, ?, ?, ?)
            """,
            (
                run_id,
                item_id,
                to_qty(planned_base),
                to_qty(actual_base),
                f"{var_pct:.2f}",
                flagged,
                line["base_unit"],
                cost_p,
            ),
        )

        if actual_base > 0:
            bal = get_stock_balance(conn, item_id)
            if bal < actual_base:
                raise ValueError(
                    f"Insufficient stock for {line['item_name']}: available {to_qty(bal)}, requested {to_qty(actual_base)}"
                )
            conn.execute(
                """
                INSERT INTO stock_ledger (
                    item_id, txn_date, movement_type, qty_delta, unit, unit_cost_paisa, total_cost_paisa,
                    source_type, source_id, notes, created_by, created_at
                ) VALUES (?, ?, 'PRODUCTION_ISSUE', ?, ?, ?, ?, 'PRODUCTION_RUN', ?, ?, ?, ?)
                """,
                (
                    item_id,
                    r_date,
                    to_qty(-actual_base),
                    line["base_unit"],
                    int(wac_paisa.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
                    cost_p,
                    run_id,
                    f"Issued to {run_no}",
                    user_id,
                    ts,
                ),
            )

        consumptions_out.append(
            {
                "item_id": item_id,
                "item_name": line["item_name"],
                "planned_qty": to_qty(planned_base),
                "actual_qty": to_qty(actual_base),
                "variance_pct": f"{var_pct:.2f}",
                "flagged": bool(flagged),
                "unit": line["base_unit"],
            }
        )

    conn.execute(
        "UPDATE production_runs SET material_cost_paisa = ? WHERE id = ?",
        (material_cost_total, run_id),
    )
    log_audit(
        conn,
        user_id,
        "START_PRODUCTION",
        "PRODUCTION_RUN",
        run_id,
        f"Started {run_no} ({bom['name']}, expected {expected_out} pkts)",
    )
    conn.commit()
    return {
        "run_id": run_id,
        "run_no": run_no,
        "expected_output_packets": expected_out,
        "consumptions": consumptions_out,
    }


def complete_production_run(
    conn: sqlite3.Connection,
    run_id: int,
    actual_output_packets: int,
    packaging_usage: Optional[Dict[int, Dict[str, Any]]] = None,
    labour_rupees: Decimal | str = "0",
    overhead_rupees: Decimal | str = "0",
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Complete a production run:
    - Record actual good output packets
    - Record packaging used + damaged (deducts from stock_ledger)
    - Allocate labour & gas/power overheads
    - Compute yield variance & flag if beyond BOM tolerance
    - Compute cost per good packet & add finished goods to stock_ledger
    """
    run = conn.execute("SELECT * FROM production_runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError("Production run not found")
    if run["status"] == "COMPLETED":
        raise ValueError("Production run already completed")

    bom = conn.execute("SELECT * FROM boms WHERE id = ?", (run["bom_id"],)).fetchone()
    tolerance = dec(bom["tolerance_pct"])
    expected_out = run["expected_output_packets"]
    yield_var = ((Decimal(actual_output_packets) - Decimal(expected_out)) / Decimal(expected_out)) * Decimal("100")
    yield_flagged = 1 if abs(yield_var) > tolerance else 0

    ts = now_iso()
    r_date = run["run_date"]
    packaging_usage = packaging_usage or {}

    for item_id, usage in packaging_usage.items():
        used_qty = dec(usage.get("used", "0"))
        damaged_qty = dec(usage.get("damaged", "0"))
        total_pkg = used_qty + damaged_qty

        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            continue
        wac_paisa = get_weighted_avg_cost_paisa(conn, item_id)
        cost_p = int((total_pkg * wac_paisa).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

        cons = conn.execute(
            "SELECT * FROM production_consumptions WHERE run_id = ? AND item_id = ?",
            (run_id, item_id),
        ).fetchone()
        planned_q = Decimal(cons["planned_qty"]) if cons else Decimal("0")
        var_pct = Decimal("0")
        flagged = 0
        if planned_q > 0:
            var_pct = ((total_pkg - planned_q) / planned_q) * Decimal("100")
            if abs(var_pct) > tolerance:
                flagged = 1

        if cons:
            conn.execute(
                """
                UPDATE production_consumptions
                SET actual_qty = ?, damaged_qty = ?, variance_pct = ?, flagged = ?, cost_paisa = ?
                WHERE id = ?
                """,
                (
                    to_qty(used_qty),
                    to_qty(damaged_qty),
                    f"{var_pct:.2f}",
                    flagged,
                    cost_p,
                    cons["id"],
                ),
            )

        if used_qty > 0:
            used_cost = int((used_qty * wac_paisa).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            conn.execute(
                """
                INSERT INTO stock_ledger (
                    item_id, txn_date, movement_type, qty_delta, unit, unit_cost_paisa, total_cost_paisa,
                    source_type, source_id, notes, created_by, created_at
                ) VALUES (?, ?, 'PRODUCTION_ISSUE', ?, ?, ?, ?, 'PRODUCTION_RUN', ?, ?, ?, ?)
                """,
                (
                    item_id,
                    r_date,
                    to_qty(-used_qty),
                    item["base_unit"],
                    int(wac_paisa.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
                    used_cost,
                    run_id,
                    f"Packaging used in {run['run_no']}",
                    user_id,
                    ts,
                ),
            )
        if damaged_qty > 0:
            dmg_cost = int((damaged_qty * wac_paisa).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            conn.execute(
                """
                INSERT INTO stock_ledger (
                    item_id, txn_date, movement_type, qty_delta, unit, unit_cost_paisa, total_cost_paisa,
                    source_type, source_id, notes, created_by, created_at
                ) VALUES (?, ?, 'DAMAGE_WASTAGE', ?, ?, ?, ?, 'PRODUCTION_RUN', ?, ?, ?, ?)
                """,
                (
                    item_id,
                    r_date,
                    to_qty(-damaged_qty),
                    item["base_unit"],
                    int(wac_paisa.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
                    dmg_cost,
                    run_id,
                    f"Damaged in {run['run_no']}",
                    user_id,
                    ts,
                ),
            )

    # Recompute total material + packaging cost for the run
    mat_cost = conn.execute(
        "SELECT COALESCE(SUM(cost_paisa), 0) FROM production_consumptions WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]
    labour_p = to_paisa(labour_rupees)
    overhead_p = to_paisa(overhead_rupees)
    total_cost_p = mat_cost + labour_p + overhead_p
    cost_per_pkt_p = (
        int((Decimal(total_cost_p) / Decimal(actual_output_packets)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if actual_output_packets > 0
        else 0
    )

    conn.execute(
        """
        UPDATE production_runs
        SET actual_output_packets = ?, yield_variance_pct = ?, yield_flagged = ?,
            labour_cost_paisa = ?, overhead_cost_paisa = ?, material_cost_paisa = ?,
            total_cost_paisa = ?, cost_per_packet_paisa = ?, status = 'COMPLETED'
        WHERE id = ?
        """,
        (
            actual_output_packets,
            f"{yield_var:.2f}",
            yield_flagged,
            labour_p,
            overhead_p,
            mat_cost,
            total_cost_p,
            cost_per_pkt_p,
            run_id,
        ),
    )

    # Add finished goods to stock_ledger
    fg_item = conn.execute("SELECT * FROM items WHERE id = ?", (bom["finished_item_id"],)).fetchone()
    conn.execute(
        """
        INSERT INTO stock_ledger (
            item_id, txn_date, movement_type, qty_delta, unit, unit_cost_paisa, total_cost_paisa,
            source_type, source_id, batch_no, notes, created_by, created_at
        ) VALUES (?, ?, 'PRODUCTION_OUTPUT', ?, ?, ?, ?, 'PRODUCTION_RUN', ?, ?, ?, ?, ?)
        """,
        (
            fg_item["id"],
            r_date,
            str(actual_output_packets),
            fg_item["base_unit"],
            cost_per_pkt_p,
            total_cost_p,
            run_id,
            run["run_no"],
            f"Output from {run['run_no']}",
            user_id,
            ts,
        ),
    )

    log_audit(
        conn,
        user_id,
        "COMPLETE_PRODUCTION",
        "PRODUCTION_RUN",
        run_id,
        f"Completed {run['run_no']}: {actual_output_packets} pkts (yield var {yield_var:.2f}%), cost/pkt ₹{from_paisa(cost_per_pkt_p)}",
    )
    conn.commit()
    return {
        "run_id": run_id,
        "run_no": run["run_no"],
        "actual_output_packets": actual_output_packets,
        "yield_variance_pct": f"{yield_var:.2f}",
        "yield_flagged": bool(yield_flagged),
        "material_cost_rupees": from_paisa(mat_cost),
        "labour_cost_rupees": from_paisa(labour_p),
        "overhead_cost_rupees": from_paisa(overhead_p),
        "total_cost_rupees": from_paisa(total_cost_p),
        "cost_per_packet_rupees": from_paisa(cost_per_pkt_p),
        "finished_goods_balance": to_qty(get_stock_balance(conn, fg_item["id"])),
    }


def add_run_overheads(
    conn: sqlite3.Connection,
    run_id: int,
    labour_rupees: Decimal | str,
    overhead_rupees: Decimal | str,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Update labour & gas/power overheads on a completed run and recalculate cost per packet."""
    run = conn.execute("SELECT * FROM production_runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError("Production run not found")
    labour_p = to_paisa(labour_rupees)
    overhead_p = to_paisa(overhead_rupees)
    total_cost_p = run["material_cost_paisa"] + labour_p + overhead_p
    actual_out = run["actual_output_packets"]
    cost_per_pkt_p = (
        int((Decimal(total_cost_p) / Decimal(actual_out)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if actual_out > 0
        else 0
    )
    conn.execute(
        """
        UPDATE production_runs
        SET labour_cost_paisa = ?, overhead_cost_paisa = ?, total_cost_paisa = ?, cost_per_packet_paisa = ?
        WHERE id = ?
        """,
        (labour_p, overhead_p, total_cost_p, cost_per_pkt_p, run_id),
    )
    conn.execute(
        """
        UPDATE stock_ledger
        SET unit_cost_paisa = ?, total_cost_paisa = ?
        WHERE source_type = 'PRODUCTION_RUN' AND source_id = ? AND movement_type = 'PRODUCTION_OUTPUT'
        """,
        (cost_per_pkt_p, total_cost_p, run_id),
    )
    log_audit(
        conn,
        user_id,
        "UPDATE_RUN_OVERHEADS",
        "PRODUCTION_RUN",
        run_id,
        f"Added overheads to {run['run_no']}: labour ₹{from_paisa(labour_p)}, power/gas ₹{from_paisa(overhead_p)}, cost/pkt ₹{from_paisa(cost_per_pkt_p)}",
    )
    conn.commit()
    return {
        "run_id": run_id,
        "run_no": run["run_no"],
        "material_cost_rupees": from_paisa(run["material_cost_paisa"]),
        "labour_cost_rupees": from_paisa(labour_p),
        "overhead_cost_rupees": from_paisa(overhead_p),
        "total_cost_rupees": from_paisa(total_cost_p),
        "cost_per_packet_rupees": from_paisa(cost_per_pkt_p),
    }


def create_sales_order(
    conn: sqlite3.Connection,
    customer_id: int,
    salesman_id: int,
    lines: List[Dict[str, Any]],
    order_date: Optional[str] = None,
    notes: str = "",
    client_offline_id: str = "",
) -> Dict[str, Any]:
    if client_offline_id:
        existing = conn.execute(
            "SELECT * FROM sales_orders WHERE client_offline_id = ?", (client_offline_id,)
        ).fetchone()
        if existing:
            return {"order_id": existing["id"], "order_no": existing["order_no"], "deduplicated": True}

    cust = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not cust:
        raise ValueError("Customer not found")

    o_date = order_date or today_iso()
    ts = now_iso()
    count = conn.execute("SELECT COUNT(*) FROM sales_orders").fetchone()[0] + 1
    order_no = f"SO-{o_date.replace('-', '')}-{count:03d}"

    shortages = []
    for ln in lines:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (ln["item_id"],)).fetchone()
        if not item:
            raise ValueError(f"Item {ln['item_id']} not found")
        bal = get_stock_balance(conn, item["id"])
        req = Decimal(str(ln["qty"]))
        if bal < req:
            shortage_qty = req - bal
            shortages.append(
                f"{item['name']}: requested {to_qty(req)}, available {to_qty(bal)}, shortage {to_qty(shortage_qty)}"
            )

    shortage_flag = 1 if shortages else 0
    shortage_text = "; ".join(shortages)

    cur = conn.execute(
        """
        INSERT INTO sales_orders (
            order_no, customer_id, salesman_id, order_date, status,
            stock_shortage_flagged, shortage_details, notes, client_offline_id, created_at
        ) VALUES (?, ?, ?, ?, 'SUBMITTED', ?, ?, ?, ?, ?)
        """,
        (order_no, customer_id, salesman_id, o_date, shortage_flag, shortage_text, notes, client_offline_id, ts),
    )
    order_id = cur.lastrowid

    for ln in lines:
        item = conn.execute("SELECT * FROM items WHERE id = ?", (ln["item_id"],)).fetchone()
        price_p = to_paisa(ln["unit_price"]) if "unit_price" in ln and ln["unit_price"] is not None else item["selling_price_paisa"]
        conn.execute(
            "INSERT INTO sales_order_lines (order_id, item_id, qty, unit_price_paisa) VALUES (?, ?, ?, ?)",
            (order_id, item["id"], int(ln["qty"]), price_p),
        )

    log_audit(
        conn,
        salesman_id,
        "CREATE_ORDER",
        "SALES_ORDER",
        order_id,
        f"Created {order_no} for {cust['name']}" + (f" [SHORTAGE: {shortage_text}]" if shortage_flag else ""),
    )
    conn.commit()
    return {
        "order_id": order_id,
        "order_no": order_no,
        "status": "SUBMITTED",
        "stock_shortage_flagged": bool(shortage_flag),
        "shortage_details": shortage_text,
        "is_interstate": cust["state"].strip().lower() != FACTORY_STATE.lower(),
    }


def check_order_stock(conn: sqlite3.Connection, order_id: int) -> Dict[str, Any]:
    order = conn.execute("SELECT * FROM sales_orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise ValueError("Order not found")
    cust = conn.execute("SELECT * FROM customers WHERE id = ?", (order["customer_id"],)).fetchone()
    lines = conn.execute(
        """
        SELECT sol.*, i.name AS item_name, i.base_unit, i.gst_rate_bps
        FROM sales_order_lines sol JOIN items i ON i.id = sol.item_id
        WHERE sol.order_id = ?
        """,
        (order_id,),
    ).fetchall()
    details = []
    sufficient = True
    for ln in lines:
        bal = get_stock_balance(conn, ln["item_id"])
        req = Decimal(ln["qty"])
        shortage = max(Decimal("0"), req - bal)
        if shortage > 0:
            sufficient = False
        details.append(
            {
                "item_id": ln["item_id"],
                "item_name": ln["item_name"],
                "requested": ln["qty"],
                "available": to_qty(bal),
                "shortage": to_qty(shortage),
            }
        )
    return {
        "order_id": order_id,
        "order_no": order["order_no"],
        "customer_name": cust["name"],
        "place_of_supply": cust["state"],
        "is_interstate": cust["state"].strip().lower() != FACTORY_STATE.lower(),
        "tax_type": "IGST" if cust["state"].strip().lower() != FACTORY_STATE.lower() else "CGST + SGST",
        "sufficient_stock": sufficient,
        "lines": details,
    }


def approve_and_invoice_order(
    conn: sqlite3.Connection,
    order_id: int,
    admin_id: Optional[int] = None,
    dispatch_immediately: bool = True,
    invoice_date: Optional[str] = None,
) -> Dict[str, Any]:
    order = conn.execute("SELECT * FROM sales_orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        raise ValueError("Order not found")
    if order["status"] in ("INVOICED", "DISPATCHED", "REJECTED"):
        raise ValueError(f"Order cannot be invoiced from status {order['status']}")

    stock_check = check_order_stock(conn, order_id)
    if not stock_check["sufficient_stock"]:
        short_msgs = [
            f"{ln['item_name']} (short by {ln['shortage']})"
            for ln in stock_check["lines"]
            if Decimal(ln["shortage"]) > 0
        ]
        raise ValueError(
            f"Insufficient finished-goods stock: {', '.join(short_msgs)}. "
            f"Note: {stock_check['tax_type']} applies if invoiced."
        )

    cust = conn.execute("SELECT * FROM customers WHERE id = ?", (order["customer_id"],)).fetchone()
    lines = conn.execute(
        """
        SELECT sol.*, i.name AS item_name, i.gst_rate_bps, i.base_unit
        FROM sales_order_lines sol JOIN items i ON i.id = sol.item_id
        WHERE sol.order_id = ?
        """,
        (order_id,),
    ).fetchall()

    inv_date = invoice_date or today_iso()
    fy = get_financial_year(inv_date)
    count = conn.execute("SELECT COUNT(*) FROM invoices WHERE financial_year = ?", (fy,)).fetchone()[0] + 1
    invoice_no = f"{fy}/INV-{count:04d}"

    is_interstate = 1 if cust["state"].strip().lower() != FACTORY_STATE.lower() else 0
    taxable_total = 0
    cgst_total = 0
    sgst_total = 0
    igst_total = 0

    for ln in lines:
        line_taxable = ln["qty"] * ln["unit_price_paisa"]
        gst_bps = ln["gst_rate_bps"]
        line_gst = int(
            (Decimal(line_taxable) * Decimal(gst_bps) / Decimal(10000)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        taxable_total += line_taxable
        if is_interstate:
            igst_total += line_gst
        else:
            half_gst = line_gst // 2
            cgst_total += half_gst
            sgst_total += line_gst - half_gst

    total_paisa = taxable_total + cgst_total + sgst_total + igst_total
    ts = now_iso()
    inv_status = "DISPATCHED" if dispatch_immediately else "POSTED"
    order_status = "DISPATCHED" if dispatch_immediately else "INVOICED"

    cur = conn.execute(
        """
        INSERT INTO invoices (
            invoice_no, order_id, customer_id, invoice_date, financial_year,
            place_of_supply, is_interstate, taxable_paisa, cgst_paisa, sgst_paisa, igst_paisa,
            total_paisa, status, dispatched_at, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            invoice_no,
            order_id,
            cust["id"],
            inv_date,
            fy,
            cust["state"],
            is_interstate,
            taxable_total,
            cgst_total,
            sgst_total,
            igst_total,
            total_paisa,
            inv_status,
            ts if dispatch_immediately else "",
            admin_id,
            ts,
        ),
    )
    invoice_id = cur.lastrowid

    conn.execute("UPDATE sales_orders SET status = ? WHERE id = ?", (order_status, order_id))

    if dispatch_immediately:
        for ln in lines:
            wac_p = get_weighted_avg_cost_paisa(conn, ln["item_id"])
            cost_p = int((Decimal(ln["qty"]) * wac_p).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            conn.execute(
                """
                INSERT INTO stock_ledger (
                    item_id, txn_date, movement_type, qty_delta, unit, unit_cost_paisa, total_cost_paisa,
                    source_type, source_id, notes, created_by, created_at
                ) VALUES (?, ?, 'SALES_DISPATCH', ?, ?, ?, ?, 'INVOICE', ?, ?, ?, ?)
                """,
                (
                    ln["item_id"],
                    inv_date,
                    str(-ln["qty"]),
                    ln["base_unit"],
                    int(wac_p.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
                    cost_p,
                    invoice_id,
                    f"Dispatched {invoice_no} to {cust['name']}",
                    admin_id,
                    ts,
                ),
            )

    log_audit(
        conn,
        admin_id,
        "APPROVE_AND_INVOICE",
        "INVOICE",
        invoice_id,
        f"Raised {invoice_no} for {cust['name']} (Total ₹{from_paisa(total_paisa)}, status={inv_status})",
    )
    conn.commit()
    return {
        "invoice_id": invoice_id,
        "invoice_no": invoice_no,
        "taxable_rupees": from_paisa(taxable_total),
        "cgst_rupees": from_paisa(cgst_total),
        "sgst_rupees": from_paisa(sgst_total),
        "igst_rupees": from_paisa(igst_total),
        "total_rupees": from_paisa(total_paisa),
        "status": inv_status,
    }


def cancel_invoice_with_credit_note(
    conn: sqlite3.Connection, invoice_id: int, admin_id: Optional[int] = None
) -> Dict[str, Any]:
    """Audit trail rule: posted invoices are cancelled with a credit note, never deleted."""
    inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not inv:
        raise ValueError("Invoice not found")
    if inv["status"] == "CANCELLED_BY_CREDIT_NOTE":
        raise ValueError("Invoice already cancelled")

    cn_no = f"CN-{inv['invoice_no']}"
    ts = now_iso()
    conn.execute(
        "UPDATE invoices SET status = 'CANCELLED_BY_CREDIT_NOTE', credit_note_no = ? WHERE id = ?",
        (cn_no, invoice_id),
    )
    if inv["status"] == "DISPATCHED":
        lines = conn.execute(
            """
            SELECT sol.*, i.base_unit FROM sales_order_lines sol
            JOIN items i ON i.id = sol.item_id WHERE sol.order_id = ?
            """,
            (inv["order_id"],),
        ).fetchall()
        for ln in lines:
            conn.execute(
                """
                INSERT INTO stock_ledger (
                    item_id, txn_date, movement_type, qty_delta, unit, unit_cost_paisa, total_cost_paisa,
                    source_type, source_id, notes, created_by, created_at
                ) VALUES (?, ?, 'CREDIT_RETURN', ?, ?, 0, 0, 'INVOICE', ?, ?, ?, ?)
                """,
                (
                    ln["item_id"],
                    today_iso(),
                    str(ln["qty"]),
                    ln["base_unit"],
                    invoice_id,
                    f"Credit Note {cn_no}",
                    admin_id,
                    ts,
                ),
            )
    log_audit(conn, admin_id, "CANCEL_INVOICE_CREDIT_NOTE", "INVOICE", invoice_id, f"Issued {cn_no}")
    conn.commit()
    return {"invoice_id": invoice_id, "credit_note_no": cn_no}


def record_payment(
    conn: sqlite3.Connection,
    customer_id: int,
    amount_rupees: Decimal | str,
    invoice_id: Optional[int] = None,
    payment_date: Optional[str] = None,
    mode: str = "UPI",
    reference_no: str = "",
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    cust = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not cust:
        raise ValueError("Customer not found")
    amt_p = to_paisa(amount_rupees)
    p_date = payment_date or today_iso()
    ts = now_iso()

    cur = conn.execute(
        """
        INSERT INTO payments (customer_id, invoice_id, payment_date, amount_paisa, mode, reference_no, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (customer_id, invoice_id, p_date, amt_p, mode, reference_no, user_id, ts),
    )
    pay_id = cur.lastrowid
    log_audit(
        conn,
        user_id,
        "RECORD_PAYMENT",
        "PAYMENT",
        pay_id,
        f"Received ₹{from_paisa(amt_p)} from {cust['name']} ({mode})",
    )
    conn.commit()
    outstanding = get_customer_outstanding_paisa(conn, customer_id)
    return {
        "payment_id": pay_id,
        "customer_name": cust["name"],
        "amount_rupees": from_paisa(amt_p),
        "outstanding_rupees": from_paisa(outstanding),
    }


def get_customer_outstanding_paisa(conn: sqlite3.Connection, customer_id: int) -> int:
    invoiced = conn.execute(
        """
        SELECT COALESCE(SUM(total_paisa), 0) FROM invoices
        WHERE customer_id = ? AND status != 'CANCELLED_BY_CREDIT_NOTE'
        """,
        (customer_id,),
    ).fetchone()[0]
    paid = conn.execute(
        "SELECT COALESCE(SUM(amount_paisa), 0) FROM payments WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()[0]
    return invoiced - paid
