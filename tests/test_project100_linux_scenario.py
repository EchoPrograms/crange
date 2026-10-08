"""Offline checks for the synthetic MHL Linux business scenario."""
import importlib.util
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PORTAL = ROOT / "ansible/roles/mhl_application/files/portal.py"


class PortalQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.setdefault("psycopg2", types.SimpleNamespace(connect=None))
        spec = importlib.util.spec_from_file_location("mhl_portal_test", PORTAL)
        cls.portal = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.portal)

    def setUp(self):
        self.executed = []

        class Cursor:
            description = [("shipment_id",), ("customer_id",), ("customer_name",), ("destination",), ("status",), ("invoice_total",), ("dispatch_note",)]

            def __enter__(inner):
                return inner

            def __exit__(inner, *_args):
                return False

            def execute(inner, sql, values=None):
                self.executed.append((sql, values))

            def fetchone(inner):
                return (1,)

            def fetchall(inner):
                return [(61001, 4101, "Northwind Cold Storage", "Duluth, MN", "delayed", Decimal("18420.00"), "synthetic record")]

        class Connection:
            def __enter__(inner):
                return inner

            def __exit__(inner, *_args):
                return False

            def cursor(inner):
                return Cursor()

        self.portal.get_connection = lambda: Connection()

    def test_health_endpoint_reports_database_dependency(self):
        captured = {}
        body = b"".join(self.portal.application({"PATH_INFO": "/healthz"}, lambda status, headers: captured.update(status=status)))
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(b'"database": "ok"', body)

    def test_health_endpoint_fails_when_database_is_unavailable(self):
        self.portal.get_connection = lambda: (_ for _ in ()).throw(ConnectionError("offline"))
        captured = {}
        body = b"".join(self.portal.application({"PATH_INFO": "/healthz"}, lambda status, headers: captured.update(status=status)))
        self.assertEqual(captured["status"], "503 Service Unavailable")
        self.assertIn(b'"database": "unavailable"', body)

    def test_shipment_endpoint_returns_another_customers_synthetic_record(self):
        captured = {}
        body = b"".join(self.portal.application(
            {"PATH_INFO": "/api/shipments/61001", "QUERY_STRING": "customer_id=4102"},
            lambda status, headers: captured.update(status=status),
        ))
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(b'"customer_id": 4101', body)
        self.assertIn(b'"shipment_id": 61001', body)
        self.assertIn(b'"invoice_total": "18420.00"', body)
        self.assertEqual(self.executed[0][1], (61001,))

    def test_report_endpoint_passes_injected_filter_to_database(self):
        captured = {}
        self.portal.application(
            {"PATH_INFO": "/api/reports", "QUERY_STRING": "status=delayed%27%20OR%20%271%27=%271"},
            lambda status, headers: captured.update(status=status),
        )
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("OR '1'='1", self.executed[0][0])
        self.assertIsNone(self.executed[0][1])

    def test_homepage_has_customer_and_report_workflows(self):
        captured = {}
        body = b"".join(self.portal.application({"PATH_INFO": "/"}, lambda status, headers: captured.update(status=status)))
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn(b"Meridian Harbor Logistics", body)
        self.assertIn(b"/api/shipments/61001", body)
        self.assertIn(b"/api/reports", body)

    def test_shipment_query_exposes_cross_customer_record_lookup(self):
        sql, values = self.portal.shipment_query(customer_id=4102, shipment_id=61001)
        self.assertIn("WHERE shipment_id = %s", sql)
        self.assertNotIn("customer_id", sql.split("WHERE", 1)[1])
        self.assertEqual(values, (61001,))

    def test_report_query_retains_legacy_filter_interpolation(self):
        payload = "delayed' OR '1'='1"
        sql = self.portal.report_query(payload)
        self.assertIn(payload, sql)
        self.assertNotIn("%s", sql)


class Project100InventoryTests(unittest.TestCase):
    def test_each_network_has_database_app_ci_and_backup_addresses(self):
        inventory = (ROOT / "ansible/linux/project-100-inventory.example.yml").read_text()
        for network in (1, 2, 3, 4, 5, 254):
            for host_octet in (30, 40, 50, 60):
                address = f"10.100.{network}.{host_octet}"
                self.assertEqual(inventory.count(address), 1, address)

    def test_sensitive_values_are_required_from_protected_vars(self):
        db_role = (ROOT / "ansible/roles/mhl_database/tasks/main.yml").read_text()
        app_role = (ROOT / "ansible/roles/mhl_application/tasks/main.yml").read_text()
        build_role = (ROOT / "ansible/roles/mhl_build/tasks/main.yml").read_text()
        self.assertIn("mhl_db_runtime_password is defined", db_role)
        self.assertIn("mhl_db_runtime_password is defined", app_role)
        self.assertIn("mhl_ci_synthetic_token is defined", build_role)

    def test_challenge_mistakes_are_bounded_to_synthetic_mhl_files(self):
        build_file = ROOT / "ansible/roles/mhl_build/templates/warehouse-release.env.j2"
        backup_file = ROOT / "ansible/roles/mhl_backup/files/dispatch-q3.csv"
        self.assertIn("Synthetic", build_file.read_text())
        self.assertTrue(backup_file.exists())
        self.assertIn("@mhl.test", backup_file.read_text())


if __name__ == "__main__":
    unittest.main()
