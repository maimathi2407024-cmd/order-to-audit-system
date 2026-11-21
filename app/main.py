from pathlib import Path
from typing import Any, Dict, List, Optional
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.db import DEFAULT_DB_PATH, get_connection, init_db, to_paisa
from app.reports import (
    export_csv_bytes,
    export_excel_bytes,
    generate_invoice_pdf,
    generate_monthly_audit_pdf,
    get_gst_summary,
    get_production_report,
    get_purchase_register,
    get_receivables_ageing,
    get_sales_register,
    get_stock_ledger_report,
)
from app.services import (
    add_run_overheads,
    approve_and_invoice_order,
    cancel_invoice_with_credit_note,
    check_order_stock,
    complete_production_run,
    create_sales_order,
    get_all_stock_summary,
    hash_password,
    record_payment,
    record_purchase,
    start_production_run,
)
from seed_benchmark import reset_and_seed

app = FastAPI(title="Order-to-Audit System", version="1.0.0")

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Simple in-memory session tokens (for local/demo RBAC)
SESSIONS: Dict[str, Dict[str, Any]] = {}


def get_db():
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@app.on_event("startup")
def startup_event():
    if not DEFAULT_DB_PATH.exists():
        c = reset_and_seed(DEFAULT_DB_PATH)
        c.close()
    else:
        c = get_connection(DEFAULT_DB_PATH)
        init_db(c)
        c.close()


def get_current_user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = authorization.split(" ", 1)[1].strip()
    user = SESSIONS.get(token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return user


def require_roles(*roles: str):
    def checker(user: Dict[str, Any] = Depends(get_current_user)):
        if user["role"] not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Role '{user['role']}' not permitted. Required: {', '.join(roles)}",
            )
        return user

    return checker


class LoginReq(BaseModel):
    username: str
    password: str = "demo123"


class PurchaseReq(BaseModel):
    item_id: int
    qty: str
    unit: str = "kg"
    rate_rupees: str
    supplier_id: Optional[int] = None
    invoice_no: str = ""
    batch_no: str = ""
    expiry_date: str = ""


class StartRunReq(BaseModel):
    bom_id: int
    batches: str = "1"
    actual_issues: Dict[int, Dict[str, Any]] = {}
    notes: str = ""


class CompleteRunReq(BaseModel):
    actual_output_packets: int
    packaging_usage: Dict[int, Dict[str, Any]] = {}
    labour_rupees: str = "0"
    overhead_rupees: str = "0"


class OverheadsReq(BaseModel):
    labour_rupees: str
    overhead_rupees: str


class OrderLineReq(BaseModel):
    item_id: int
    qty: int
    unit_price: Optional[str] = None


class CreateOrderReq(BaseModel):
    customer_id: int
    lines: List[OrderLineReq]
    notes: str = ""
    client_offline_id: str = ""


class PaymentReq(BaseModel):
    customer_id: int
    invoice_id: Optional[int] = None
    amount_rupees: str
    mode: str = "UPI"
    reference_no: str = ""


@app.get("/")
def serve_ui():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/api/auth/login")
def login(req: LoginReq, conn=Depends(get_db)):
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? AND active = 1", (req.username,)
    ).fetchone()
    if not row or row["password_hash"] != hash_password(req.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = secrets.token_hex(24)
    user_info = {
        "id": row["id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "role": row["role"],
    }
    SESSIONS[token] = user_info
    return {"token": token, "user": user_info}


@app.get("/api/auth/me")
def me(user=Depends(get_current_user)):
    return user


@app.post("/api/admin/reset-benchmark")
def reset_benchmark(user=Depends(require_roles("admin", "owner"))):
    c = reset_and_seed(DEFAULT_DB_PATH)
    c.close()
    return {"status": "ok", "message": "Database reset to Section 5 opening state"}


@app.get("/api/masters")
def get_masters(user=Depends(get_current_user), conn=Depends(get_db)):
    items = [dict(r) for r in conn.execute("SELECT * FROM items ORDER BY id").fetchall()]
    customers = [dict(r) for r in conn.execute("SELECT * FROM customers ORDER BY id").fetchall()]
    suppliers = [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY id").fetchall()]
    boms = []
    for b in conn.execute("SELECT * FROM boms ORDER BY id").fetchall():
        bd = dict(b)
        bd["lines"] = [
            dict(ln)
            for ln in conn.execute(
                """
                SELECT bl.*, i.name AS item_name, i.category, i.base_unit
                FROM bom_lines bl JOIN items i ON i.id = bl.item_id
                WHERE bl.bom_id = ?
                """,
                (b["id"],),
            ).fetchall()
        ]
        boms.append(bd)
    return {"items": items, "customers": customers, "suppliers": suppliers, "boms": boms}


@app.get("/api/stock/summary")
def stock_summary(user=Depends(get_current_user), conn=Depends(get_db)):
    return get_all_stock_summary(conn)


@app.get("/api/stock/ledger")
def stock_ledger(
    item_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    user=Depends(require_roles("admin", "production", "auditor", "owner")),
    conn=Depends(get_db),
):
    return get_stock_ledger_report(conn, item_id, start_date, end_date)


@app.get("/api/purchases")
def list_purchases(user=Depends(require_roles("admin", "production", "auditor", "owner")), conn=Depends(get_db)):
 return get_purchase_register(conn)

@app.post("/api/purchases")
def create_purchase(
    req: PurchaseReq,
    user=Depends(require_roles("admin", "production", "owner")),
    conn=Depends(get_db),
):
    try:
        return record_purchase(
            conn,
            item_id=req.item_id,
            qty=req.qty,
            unit=req.unit,
            rate_rupees=req.rate_rupees,
            supplier_id=req.supplier_id,
            invoice_no=req.invoice_no,
            batch_no=req.batch_no,
            expiry_date=req.expiry_date,
            user_id=user["id"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/production/runs")
def list_runs(
    user=Depends(require_roles("admin", "production", "auditor", "owner")),
    conn=Depends(get_db),
):
    return get_production_report(conn)


@app.post("/api/production/runs/start")
def api_start_run(
    req: StartRunReq,
    user=Depends(require_roles("production", "admin", "owner")),
    conn=Depends(get_db),
):
    try:
        return start_production_run(
            conn,
            bom_id=req.bom_id,
            batches=req.batches,
            actual_issues=req.actual_issues,
            notes=req.notes,
            user_id=user["id"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/production/runs/{run_id}/complete")
def api_complete_run(
    run_id: int,
    req: CompleteRunReq,
    user=Depends(require_roles("production", "admin", "owner")),
    conn=Depends(get_db),
):
    try:
        return complete_production_run(
            conn,
            run_id=run_id,
            actual_output_packets=req.actual_output_packets,
            packaging_usage=req.packaging_usage,
            labour_rupees=req.labour_rupees,
            overhead_rupees=req.overhead_rupees,
            user_id=user["id"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/production/runs/{run_id}/overheads")
def api_add_overheads(
    run_id: int,
    req: OverheadsReq,
    user=Depends(require_roles("production", "admin", "owner")),
    conn=Depends(get_db),
):
    try:
        return add_run_overheads(
            conn, run_id, req.labour_rupees, req.overhead_rupees, user_id=user["id"]
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/orders")
def list_orders(user=Depends(get_current_user), conn=Depends(get_db)):
    query = """
        SELECT so.*, c.name AS customer_name, c.state AS customer_state,
               c.customer_type, u.full_name AS salesman_name
        FROM sales_orders so
        JOIN customers c ON c.id = so.customer_id
        JOIN users u ON u.id = so.salesman_id
    """
    params: List[Any] = []
    if user["role"] == "salesman":
        query += " WHERE so.salesman_id = ?"
        params.append(user["id"])
    query += " ORDER BY so.id DESC"
    orders = []
    for r in conn.execute(query, params).fetchall():
        od = dict(r)
        od["lines"] = [
            dict(ln)
            for ln in conn.execute(
                """
                SELECT sol.*, i.name AS item_name, i.base_unit
                FROM sales_order_lines sol JOIN items i ON i.id = sol.item_id
                WHERE sol.order_id = ?
                """,
                (r["id"],),
            ).fetchall()
        ]
        orders.append(od)
    return orders


@app.post("/api/orders")
def api_create_order(
    req: CreateOrderReq,
    user=Depends(require_roles("salesman", "admin", "owner")),
    conn=Depends(get_db),
):
    try:
        return create_sales_order(
            conn,
            customer_id=req.customer_id,
            salesman_id=user["id"],
            lines=[ln.dict() for ln in req.lines],
            notes=req.notes,
            client_offline_id=req.client_offline_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/orders/{order_id}/stock-check")
def api_check_order_stock(
    order_id: int,
    user=Depends(require_roles("admin", "owner")),
    conn=Depends(get_db),
):
    try:
        return check_order_stock(conn, order_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/orders/{order_id}/approve-invoice")
def api_approve_invoice(
    order_id: int,
    dispatch_immediately: bool = True,
    user=Depends(require_roles("admin", "owner")),
    conn=Depends(get_db),
):
    try:
        return approve_and_invoice_order(
            conn, order_id, admin_id=user["id"], dispatch_immediately=dispatch_immediately
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/invoices")
def list_invoices(
    user=Depends(require_roles("admin", "auditor", "owner", "salesman")),
    conn=Depends(get_db),
):
    return get_sales_register(conn)


@app.post("/api/invoices/{invoice_id}/credit-note")
def api_credit_note(
    invoice_id: int,
    user=Depends(require_roles("admin", "owner")),
    conn=Depends(get_db),
):
    try:
        return cancel_invoice_with_credit_note(conn, invoice_id, admin_id=user["id"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/invoices/{invoice_id}/pdf")
def api_invoice_pdf(invoice_id: int, conn=Depends(get_db)):
    try:
        pdf_bytes = generate_invoice_pdf(conn, invoice_id)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=invoice_{invoice_id}.pdf"},
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/payments")
def api_record_payment(
    req: PaymentReq,
    user=Depends(require_roles("admin", "owner")),
    conn=Depends(get_db),
):
    try:
        return record_payment(
            conn,
            customer_id=req.customer_id,
            amount_rupees=req.amount_rupees,
            invoice_id=req.invoice_id,
            mode=req.mode,
            reference_no=req.reference_no,
            user_id=user["id"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/reports/all")
def api_all_reports(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    user=Depends(require_roles("admin", "auditor", "owner")),
    conn=Depends(get_db),
):
    return {
        "sales_register": get_sales_register(conn, start_date, end_date),
        "gst_summary": get_gst_summary(conn, start_date, end_date),
        "purchase_register": get_purchase_register(conn, start_date, end_date),
        "stock_summary": get_all_stock_summary(conn),
        "stock_ledger": get_stock_ledger_report(conn, None, start_date, end_date),
        "production_report": get_production_report(conn, start_date, end_date),
        "receivables_ageing": get_receivables_ageing(conn),
    }


@app.get("/api/reports/export/excel")
def api_export_excel(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    conn=Depends(get_db),
):
    sheets = {
        "Sales Register": get_sales_register(conn, start_date, end_date),
        "GST Summary": get_gst_summary(conn, start_date, end_date)["buckets"],
        "Purchase Register": get_purchase_register(conn, start_date, end_date),
        "Stock Summary": get_all_stock_summary(conn),
        "Stock Ledger": get_stock_ledger_report(conn, None, start_date, end_date),
        "Production Report": get_production_report(conn, start_date, end_date),
        "Receivables Ageing": get_receivables_ageing(conn),
    }
    xls = export_excel_bytes(sheets)
    return Response(
        content=xls,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=monthly_audit_reports.xlsx"},
    )


@app.get("/api/reports/export/csv/{report_name}")
def api_export_csv(report_name: str, conn=Depends(get_db)):
    mapping = {
        "sales": get_sales_register(conn),
        "purchases": get_purchase_register(conn),
        "stock": get_all_stock_summary(conn),
        "ledger": get_stock_ledger_report(conn),
        "receivables": get_receivables_ageing(conn),
    }
    if report_name not in mapping:
        raise HTTPException(status_code=404, detail="Unknown report")
    csv_b = export_csv_bytes(mapping[report_name])
    return Response(
        content=csv_b,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={report_name}.csv"},
    )


@app.get("/api/reports/export/pdf")
def api_export_pdf(conn=Depends(get_db)):
    pdf_b = generate_monthly_audit_pdf(conn)
    return Response(
        content=pdf_b,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=monthly_audit_report.pdf"},
    )


@app.get("/api/audit-logs")
def api_audit_logs(
    user=Depends(require_roles("admin", "auditor", "owner")),
    conn=Depends(get_db),
):
    rows = conn.execute(
        """
        SELECT al.*, COALESCE(u.full_name, 'System') AS user_name
        FROM audit_logs al LEFT JOIN users u ON u.id = al.user_id
        ORDER BY al.id DESC LIMIT 100
        """
    ).fetchall()
    return [dict(r) for r in rows]
