import csv
import io
import sqlite3
from typing import Any, Dict, List, Optional

import openpyxl
from openpyxl.styles import Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.db import from_paisa
from app.services import FACTORY_GSTIN, FACTORY_STATE, get_all_stock_summary


def get_sales_register(
    conn: sqlite3.Connection, start_date: Optional[str] = None, end_date: Optional[str] = None
) -> List[Dict[str, Any]]:
    query = """
        SELECT i.invoice_no, i.invoice_date, c.name AS customer_name, c.customer_type,
               c.state AS place_of_supply, c.gstin, i.taxable_paisa, i.cgst_paisa,
               i.sgst_paisa, i.igst_paisa, i.total_paisa, i.status,
               u.full_name AS salesman_name
        FROM invoices i
        JOIN customers c ON c.id = i.customer_id
        JOIN sales_orders so ON so.id = i.order_id
        JOIN users u ON u.id = so.salesman_id
        WHERE 1=1
    """
    params: List[Any] = []
    if start_date:
        query += " AND i.invoice_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND i.invoice_date <= ?"
        params.append(end_date)
    query += " ORDER BY i.invoice_date DESC, i.id DESC"
    rows = conn.execute(query, params).fetchall()
    return [
        {
            "invoice_no": r["invoice_no"],
            "invoice_date": r["invoice_date"],
            "customer_name": r["customer_name"],
            "customer_type": r["customer_type"],
            "place_of_supply": r["place_of_supply"],
            "gstin": r["gstin"] or "URD",
            "salesman": r["salesman_name"],
            "taxable": from_paisa(r["taxable_paisa"]),
            "cgst": from_paisa(r["cgst_paisa"]),
            "sgst": from_paisa(r["sgst_paisa"]),
            "igst": from_paisa(r["igst_paisa"]),
            "total": from_paisa(r["total_paisa"]),
            "status": r["status"],
        }
        for r in rows
    ]


def get_gst_summary(
    conn: sqlite3.Connection, start_date: Optional[str] = None, end_date: Optional[str] = None
) -> Dict[str, Any]:
    query = """
        SELECT i.is_interstate, c.gstin,
               COUNT(*) AS invoice_count,
               COALESCE(SUM(i.taxable_paisa), 0) AS taxable,
               COALESCE(SUM(i.cgst_paisa), 0) AS cgst,
               COALESCE(SUM(i.sgst_paisa), 0) AS sgst,
               COALESCE(SUM(i.igst_paisa), 0) AS igst,
               COALESCE(SUM(i.total_paisa), 0) AS total
        FROM invoices i
        JOIN customers c ON c.id = i.customer_id
        WHERE i.status != 'CANCELLED_BY_CREDIT_NOTE'
    """
    params: List[Any] = []
    if start_date:
        query += " AND i.invoice_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND i.invoice_date <= ?"
        params.append(end_date)
    query += " GROUP BY i.is_interstate, (CASE WHEN c.gstin != '' THEN 'B2B' ELSE 'B2C' END)"
    rows = conn.execute(query, params).fetchall()

    buckets = []
    tot_taxable = tot_cgst = tot_sgst = tot_igst = tot_val = 0
    for r in rows:
        supply_type = "B2B (Registered)" if r["gstin"] else "B2C (Unregistered)"
        scope = "Inter-State (IGST)" if r["is_interstate"] else "Intra-State (CGST+SGST)"
        buckets.append(
            {
                "gstr_table": "4A (B2B)" if r["gstin"] else "7 (B2CS)",
                "supply_type": supply_type,
                "scope": scope,
                "invoice_count": r["invoice_count"],
                "taxable": from_paisa(r["taxable"]),
                "cgst": from_paisa(r["cgst"]),
                "sgst": from_paisa(r["sgst"]),
                "igst": from_paisa(r["igst"]),
                "total": from_paisa(r["total"]),
            }
        )
        tot_taxable += r["taxable"]
        tot_cgst += r["cgst"]
        tot_sgst += r["sgst"]
        tot_igst += r["igst"]
        tot_val += r["total"]

    return {
        "factory_gstin": FACTORY_GSTIN,
        "factory_state": FACTORY_STATE,
        "buckets": buckets,
        "gstr3b_3_1_a_outward_taxable": from_paisa(tot_taxable),
        "total_cgst": from_paisa(tot_cgst),
        "total_sgst": from_paisa(tot_sgst),
        "total_igst": from_paisa(tot_igst),
        "total_tax": from_paisa(tot_cgst + tot_sgst + tot_igst),
        "total_invoice_value": from_paisa(tot_val),
    }


def get_purchase_register(
    conn: sqlite3.Connection, start_date: Optional[str] = None, end_date: Optional[str] = None
) -> List[Dict[str, Any]]:
    query = """
        SELECT p.id, p.purchase_date, COALESCE(s.name, 'Local Cash Purchase') AS supplier_name,
               i.name AS item_name, p.qty, p.unit, p.rate_paisa, p.total_paisa, p.invoice_no, p.batch_no
        FROM purchases p
        LEFT JOIN suppliers s ON s.id = p.supplier_id
        JOIN items i ON i.id = p.item_id
        WHERE 1=1
    """
    params: List[Any] = []
    if start_date:
        query += " AND p.purchase_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND p.purchase_date <= ?"
        params.append(end_date)
    query += " ORDER BY p.purchase_date DESC, p.id DESC"
    rows = conn.execute(query, params).fetchall()
    return [
        {
            "purchase_id": r["id"],
            "date": r["purchase_date"],
            "supplier": r["supplier_name"],
            "item": r["item_name"],
            "qty": r["qty"],
            "unit": r["unit"],
            "rate": from_paisa(r["rate_paisa"]),
            "total": from_paisa(r["total_paisa"]),
            "invoice_no": r["invoice_no"],
            "batch_no": r["batch_no"],
        }
        for r in rows
    ]


def get_stock_ledger_report(
    conn: sqlite3.Connection,
    item_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    query = """
        SELECT sl.id, sl.txn_date, i.name AS item_name, i.category, sl.movement_type,
               sl.qty_delta, sl.unit, sl.unit_cost_paisa, sl.total_cost_paisa,
               sl.source_type, sl.source_id, sl.notes
        FROM stock_ledger sl
        JOIN items i ON i.id = sl.item_id
        WHERE 1=1
    """
    params: List[Any] = []
    if item_id:
        query += " AND sl.item_id = ?"
        params.append(item_id)
    if start_date:
        query += " AND sl.txn_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND sl.txn_date <= ?"
        params.append(end_date)
    query += " ORDER BY sl.id ASC"
    rows = conn.execute(query, params).fetchall()
    return [
        {
            "ledger_id": r["id"],
            "date": r["txn_date"],
            "item": r["item_name"],
            "category": r["category"],
            "movement_type": r["movement_type"],
            "qty_delta": r["qty_delta"],
            "unit": r["unit"],
            "unit_cost": from_paisa(r["unit_cost_paisa"]),
            "total_cost": from_paisa(r["total_cost_paisa"]),
            "source": f"{r['source_type']} #{r['source_id']}",
            "notes": r["notes"],
        }
        for r in rows
    ]


def get_production_report(
    conn: sqlite3.Connection, start_date: Optional[str] = None, end_date: Optional[str] = None
) -> List[Dict[str, Any]]:
    query = """
        SELECT pr.*, b.name AS bom_name, i.name AS finished_item_name, i.selling_price_paisa
        FROM production_runs pr
        JOIN boms b ON b.id = pr.bom_id
        JOIN items i ON i.id = b.finished_item_id
        WHERE 1=1
    """
    params: List[Any] = []
    if start_date:
        query += " AND pr.run_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND pr.run_date <= ?"
        params.append(end_date)
    query += " ORDER BY pr.run_date DESC, pr.id DESC"
    runs = conn.execute(query, params).fetchall()
    result = []
    for r in runs:
        cons = conn.execute(
            """
            SELECT pc.*, i.name AS item_name
            FROM production_consumptions pc
            JOIN items i ON i.id = pc.item_id
            WHERE pc.run_id = ?
            """,
            (r["id"],),
        ).fetchall()
        margin_p = r["selling_price_paisa"] - r["cost_per_packet_paisa"]
        result.append(
            {
                "run_id": r["id"],
                "run_no": r["run_no"],
                "run_date": r["run_date"],
                "product": r["finished_item_name"],
                "batches": r["batches"],
                "expected_packets": r["expected_output_packets"],
                "actual_packets": r["actual_output_packets"],
                "yield_variance_pct": r["yield_variance_pct"],
                "yield_flagged": bool(r["yield_flagged"]),
                "material_cost": from_paisa(r["material_cost_paisa"]),
                "labour_cost": from_paisa(r["labour_cost_paisa"]),
                "overhead_cost": from_paisa(r["overhead_cost_paisa"]),
                "total_cost": from_paisa(r["total_cost_paisa"]),
                "cost_per_packet": from_paisa(r["cost_per_packet_paisa"]),
                "selling_price": from_paisa(r["selling_price_paisa"]),
                "margin_per_packet": from_paisa(margin_p),
                "status": r["status"],
                "consumptions": [
                    {
                        "item": c["item_name"],
                        "planned": c["planned_qty"],
                        "actual": c["actual_qty"],
                        "damaged": c["damaged_qty"],
                        "unit": c["unit"],
                        "variance_pct": c["variance_pct"],
                        "flagged": bool(c["flagged"]),
                    }
                    for c in cons
                ],
            }
        )
    return result


def get_receivables_ageing(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    customers = conn.execute("SELECT * FROM customers ORDER BY id").fetchall()
    rows = []
    for c in customers:
        invoiced = conn.execute(
            """
            SELECT COALESCE(SUM(total_paisa), 0) FROM invoices
            WHERE customer_id = ? AND status != 'CANCELLED_BY_CREDIT_NOTE'
            """,
            (c["id"],),
        ).fetchone()[0]
        paid = conn.execute(
            "SELECT COALESCE(SUM(amount_paisa), 0) FROM payments WHERE customer_id = ?",
            (c["id"],),
        ).fetchone()[0]
        outstanding = invoiced - paid
        rows.append(
            {
                "customer_id": c["id"],
                "customer_name": c["name"],
                "customer_type": c["customer_type"],
                "state": c["state"],
                "total_invoiced": from_paisa(invoiced),
                "total_paid": from_paisa(paid),
                "outstanding": from_paisa(outstanding),
                "ageing_0_30": from_paisa(outstanding),
                "ageing_31_60": "0.00",
                "ageing_60_plus": "0.00",
            }
        )
    return rows


def export_csv_bytes(rows: List[Dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    if not rows:
        return b""
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue().encode("utf-8")


def export_excel_bytes(sheets: Dict[str, List[Dict[str, Any]]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    header_fill = PatternFill(start_color="1E3A5F", end_color="1E3A5F", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    for sheet_name, rows in sheets.items():
        ws = wb.create_sheet(title=sheet_name[:31])
        if not rows:
            ws.append(["No data available"])
            continue
        headers = list(rows[0].keys())
        ws.append(headers)
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
        for r in rows:
            ws.append([str(r.get(h, "")) for h in headers])
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 36)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def generate_invoice_pdf(conn: sqlite3.Connection, invoice_id: int) -> bytes:
    inv = conn.execute(
        """
        SELECT i.*, c.name AS customer_name, c.gstin AS customer_gstin,
               c.address AS customer_address, c.state AS customer_state, c.phone AS customer_phone
        FROM invoices i
        JOIN customers c ON c.id = i.customer_id
        WHERE i.id = ?
        """,
        (invoice_id,),
    ).fetchone()
    if not inv:
        raise ValueError("Invoice not found")
    lines = conn.execute(
        """
        SELECT sol.*, i.name AS item_name, i.hsn_code, i.gst_rate_bps, i.base_unit
        FROM sales_order_lines sol
        JOIN items i ON i.id = sol.item_id
        WHERE sol.order_id = ?
        """,
        (inv["order_id"],),
    ).fetchall()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph("<b>TAX INVOICE (GST COMPLIANT)</b>", styles["Title"]),
        Paragraph(
            f"<b>Seller:</b> South Indian Snacks Factory | State: {FACTORY_STATE} | GSTIN: {FACTORY_GSTIN}",
            styles["Normal"],
        ),
        Spacer(1, 8),
        Paragraph(
            f"<b>Invoice No:</b> {inv['invoice_no']} &nbsp;&nbsp; <b>Date:</b> {inv['invoice_date']} &nbsp;&nbsp; <b>FY:</b> {inv['financial_year']}",
            styles["Normal"],
        ),
        Paragraph(
            f"<b>Buyer:</b> {inv['customer_name']} ({inv['customer_state']}) | GSTIN: {inv['customer_gstin'] or 'Unregistered'}",
            styles["Normal"],
        ),
        Paragraph(f"<b>Place of Supply:</b> {inv['place_of_supply']}", styles["Normal"]),
        Spacer(1, 12),
    ]

    table_data = [["#", "Item", "HSN", "Qty", "Rate (INR)", "Taxable (INR)", "GST %"]]
    for idx, ln in enumerate(lines, start=1):
        taxable = ln["qty"] * ln["unit_price_paisa"]
        table_data.append(
            [
                str(idx),
                ln["item_name"],
                ln["hsn_code"],
                f"{ln['qty']} {ln['base_unit']}",
                from_paisa(ln["unit_price_paisa"]),
                from_paisa(taxable),
                f"{ln['gst_rate_bps'] / 100:.2f}%",
            ]
        )

    t = Table(table_data, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A5F")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
            ]
        )
    )
    elements.append(t)
    elements.append(Spacer(1, 12))

    summary_data = [
        ["Taxable Value", f"INR {from_paisa(inv['taxable_paisa'])}"],
        ["CGST (2.5%)", f"INR {from_paisa(inv['cgst_paisa'])}"],
        ["SGST (2.5%)", f"INR {from_paisa(inv['sgst_paisa'])}"],
        ["IGST (5.0%)", f"INR {from_paisa(inv['igst_paisa'])}"],
        ["Grand Total", f"INR {from_paisa(inv['total_paisa'])}"],
    ]
    st = Table(summary_data, colWidths=[160, 140])
    st.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ]
        )
    )
    elements.append(st)
    doc.build(elements)
    return buf.getvalue()


def generate_monthly_audit_pdf(conn: sqlite3.Connection) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()
    elements = [
        Paragraph("<b>Monthly Accounts & Audit Report</b>", styles["Title"]),
        Paragraph(f"Factory State: {FACTORY_STATE} | GSTIN: {FACTORY_GSTIN}", styles["Normal"]),
        Spacer(1, 10),
    ]

    # Stock Summary
    elements.append(Paragraph("<b>1. Stock Summary</b>", styles["Heading2"]))
    stock = get_all_stock_summary(conn)
    s_data = [["Item", "Category", "Balance", "Unit", "WAC (INR)"]]
    for s in stock:
        s_data.append([s["name"], s["category"], s["balance"], s["base_unit"], s["wac_rupees"]])
    t1 = Table(s_data, repeatRows=1)
    t1.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A5F")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ]
        )
    )
    elements.append(t1)
    elements.append(Spacer(1, 10))

    # Sales Register
    elements.append(Paragraph("<b>2. Sales Register & GST</b>", styles["Heading2"]))
    sales = get_sales_register(conn)
    sr_data = [["Invoice", "Customer", "State", "Taxable", "CGST", "SGST", "IGST", "Total"]]
    for s in sales:
        sr_data.append(
            [
                s["invoice_no"],
                s["customer_name"],
                s["place_of_supply"],
                s["taxable"],
                s["cgst"],
                s["sgst"],
                s["igst"],
                s["total"],
            ]
        )
    if len(sr_data) == 1:
        sr_data.append(["No invoices", "-", "-", "0.00", "0.00", "0.00", "0.00", "0.00"])
    t2 = Table(sr_data, repeatRows=1)
    t2.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A5F")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ]
        )
    )
    elements.append(t2)
    elements.append(Spacer(1, 10))

    # Receivables
    elements.append(Paragraph("<b>3. Customer Receivables</b>", styles["Heading2"]))
    rec = get_receivables_ageing(conn)
    r_data = [["Customer", "Invoiced (INR)", "Paid (INR)", "Outstanding (INR)"]]
    for r in rec:
        r_data.append([r["customer_name"], r["total_invoiced"], r["total_paid"], r["outstanding"]])
    t3 = Table(r_data, repeatRows=1)
    t3.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A5F")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ]
        )
    )
    elements.append(t3)

    doc.build(elements)
    return buf.getvalue()
