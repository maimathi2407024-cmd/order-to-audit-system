import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import DEFAULT_DB_PATH
from app.main import app
from app.reports import (
    export_excel_bytes,
    generate_invoice_pdf,
    generate_monthly_audit_pdf,
    get_gst_summary,
)
from app.services import (
    add_run_overheads,
    approve_and_invoice_order,
    cancel_invoice_with_credit_note,
    check_order_stock,
    complete_production_run,
    create_sales_order,
    get_customer_outstanding_paisa,
    get_stock_balance,
    record_payment,
    record_purchase,
    start_production_run,
)
from seed_benchmark import reset_and_seed


class TestSection5Benchmark(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_ota.db"
        self.conn = reset_and_seed(self.db_path)
        self.items = {r["sku"]: r["id"] for r in self.conn.execute("SELECT id, sku FROM items").fetchall()}
        self.customers = {r["name"]: r["id"] for r in self.conn.execute("SELECT id, name FROM customers").fetchall()}
        self.users = {r["username"]: r["id"] for r in self.conn.execute("SELECT id, username FROM users").fetchall()}
        self.bom_id = self.conn.execute("SELECT id FROM boms").fetchone()["id"]

    def tearDown(self):
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_section_5_end_to_end_benchmark(self):
        # Step 1: Record a purchase of 60 kg flour at ₹45/kg
        record_purchase(
            self.conn,
            item_id=self.items["RM-FLOUR"],
            qty="60",
            unit="kg",
            rate_rupees="45",
            supplier_id=1,
            invoice_no="SUP-001",
            user_id=self.users["production"],
        )

        # Step 2: Start 1 production run of Murukku 200 g (40 kg flour, 10.5 L oil, 1.2 kg spice mix)
        run = start_production_run(
            self.conn,
            bom_id=self.bom_id,
            batches="1",
            actual_issues={
                self.items["RM-FLOUR"]: {"qty": "40", "unit": "kg"},
                self.items["RM-OIL"]: {"qty": "10.5", "unit": "litre"},
                self.items["RM-SPICE"]: {"qty": "1.2", "unit": "kg"},
            },
            user_id=self.users["production"],
        )
        oil_cons = next(c for c in run["consumptions"] if c["item_id"] == self.items["RM-OIL"])
        self.assertEqual(oil_cons["variance_pct"], "5.00")
        self.assertTrue(oil_cons["flagged"])

        # Step 3: Record actual output of 188 good packets, 6 pouches damaged, 9 cartons used
        comp = complete_production_run(
            self.conn,
            run_id=run["run_id"],
            actual_output_packets=188,
            packaging_usage={
                self.items["PK-POUCH200"]: {"used": "188", "damaged": "6"},
                self.items["PK-CARTON"]: {"used": "9", "damaged": "0"},
            },
            user_id=self.users["production"],
        )
        self.assertEqual(comp["yield_variance_pct"], "-6.00")
        self.assertTrue(comp["yield_flagged"])

        # Verify stock balances after production
        self.assertEqual(str(get_stock_balance(self.conn, self.items["RM-FLOUR"])), "120")
        self.assertEqual(str(get_stock_balance(self.conn, self.items["RM-OIL"])), "19.5")
        self.assertEqual(str(get_stock_balance(self.conn, self.items["PK-POUCH200"])), "306")
        self.assertEqual(str(get_stock_balance(self.conn, self.items["FG-MURUKKU200"])), "238")

        # Step 4: Salesman A places order for Retailer A (Tamil Nadu): 150 packets
        so_a = create_sales_order(
            self.conn,
            customer_id=self.customers["Retailer A"],
            salesman_id=self.users["salesman_a"],
            lines=[{"item_id": self.items["FG-MURUKKU200"], "qty": 150}],
        )
        self.assertFalse(so_a["stock_shortage_flagged"])

        # Step 5: Admin approves, raises GST invoice (CGST + SGST), and records dispatch
        inv_a = approve_and_invoice_order(
            self.conn, so_a["order_id"], admin_id=self.users["admin"], dispatch_immediately=True
        )
        self.assertEqual(inv_a["taxable_rupees"], "7500.00")
        self.assertEqual(inv_a["cgst_rupees"], "187.50")
        self.assertEqual(inv_a["sgst_rupees"], "187.50")
        self.assertEqual(inv_a["igst_rupees"], "0.00")
        self.assertEqual(inv_a["total_rupees"], "7875.00")
        self.assertEqual(str(get_stock_balance(self.conn, self.items["FG-MURUKKU200"])), "88")

        # Step 6: Salesman B places order for Distributor B (Karnataka): 120 packets -> shortage 32 flagged
        so_b = create_sales_order(
            self.conn,
            customer_id=self.customers["Distributor B"],
            salesman_id=self.users["salesman_b"],
            lines=[{"item_id": self.items["FG-MURUKKU200"], "qty": 120}],
        )
        self.assertTrue(so_b["stock_shortage_flagged"])
        self.assertIn("shortage 32", so_b["shortage_details"])

        chk_b = check_order_stock(self.conn, so_b["order_id"])
        self.assertFalse(chk_b["sufficient_stock"])
        self.assertEqual(chk_b["tax_type"], "IGST")
        with self.assertRaises(ValueError):
            approve_and_invoice_order(self.conn, so_b["order_id"], admin_id=self.users["admin"])

        # Step 7: Record payment of ₹4,000 from Retailer A -> ₹3,875 outstanding
        pay = record_payment(
            self.conn,
            customer_id=self.customers["Retailer A"],
            amount_rupees="4000",
            invoice_id=inv_a["invoice_id"],
            user_id=self.users["admin"],
        )
        self.assertEqual(pay["outstanding_rupees"], "3875.00")
        self.assertEqual(get_customer_outstanding_paisa(self.conn, self.customers["Retailer A"]), 387500)

        # Step 8: Add labour ₹1,200 and gas/power ₹600 -> cost per packet
        ovh = add_run_overheads(
            self.conn, run["run_id"], labour_rupees="1200", overhead_rupees="600", user_id=self.users["production"]
        )
        # Material: 40*43.125 (1725) + 10.5*140 (1470) + 1.2*200 (240) + 194*2.5 (485) + 9*15 (135) = ₹4,055.00
        # Overheads: 1200 + 600 = ₹1,800.00 -> Total = ₹5,855.00 / 188 = ₹31.14
        self.assertEqual(ovh["material_cost_rupees"], "4055.00")
        self.assertEqual(ovh["total_cost_rupees"], "5855.00")
        self.assertEqual(ovh["cost_per_packet_rupees"], "31.14")

        # Step 9: Verify reports & PDF/Excel generation
        gst = get_gst_summary(self.conn)
        self.assertEqual(gst["gstr3b_3_1_a_outward_taxable"], "7500.00")
        self.assertEqual(gst["total_cgst"], "187.50")
        self.assertEqual(gst["total_sgst"], "187.50")

        pdf_inv = generate_invoice_pdf(self.conn, inv_a["invoice_id"])
        self.assertTrue(pdf_inv.startswith(b"%PDF"))
        pdf_rep = generate_monthly_audit_pdf(self.conn)
        self.assertTrue(pdf_rep.startswith(b"%PDF"))
        xls = export_excel_bytes({"GST": gst["buckets"]})
        self.assertTrue(len(xls) > 500)

        # Audit trail: credit note cancellation restores stock without deleting invoice
        cn = cancel_invoice_with_credit_note(self.conn, inv_a["invoice_id"], admin_id=self.users["admin"])
        self.assertTrue(cn["credit_note_no"].startswith("CN-"))
        self.assertEqual(str(get_stock_balance(self.conn, self.items["FG-MURUKKU200"])), "238")

    def test_api_rbac_and_ui(self):
        c = reset_and_seed(DEFAULT_DB_PATH)
        c.close()
        client = TestClient(app)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        login_res = client.post("/api/auth/login", json={"username": "salesman_a", "password": "demo123"})
        self.assertEqual(login_res.status_code, 200)
        tok = login_res.json()["token"]
        # Salesman cannot access auditor/admin stock ledger
        forbidden = client.get("/api/stock/ledger", headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(forbidden.status_code, 403)


if __name__ == "__main__":
    unittest.main()
