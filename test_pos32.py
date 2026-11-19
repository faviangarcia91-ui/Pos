import json
import tempfile
import threading
import unittest
from pathlib import Path
from database import POSDatabase

class POS32DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = POSDatabase(self.root / "pos.db")
        self.db.register_terminal("CAJA-01")

    def tearDown(self):
        self.temp.cleanup()

    def shift(self, sid="T-1"):
        return {"id": sid, "usuario": "ana", "caja_id": "CAJA-01", "estado": "abierto",
                "fecha_apertura": "2026-11-14 09:00:00", "efectivo_inicial": 100.0}

    def sale(self, invoice, shift_id="T-1"):
        return {"factura": invoice, "usuario": "ana", "caja_id": "CAJA-01", "turno_id": shift_id,
                "total": 25.0, "metodo": "Efectivo", "productos": []}

    def test_open_shift_unique_per_terminal(self):
        self.db.open_shift(self.shift())
        with self.assertRaises(ValueError):
            self.db.open_shift(self.shift("T-2"))

    def test_different_terminals_can_have_open_shifts(self):
        self.db.open_shift(self.shift("T-1"))
        second = self.shift("T-2")
        second["caja_id"] = "CAJA-02"
        self.db.open_shift(second)
        self.assertEqual(len([x for x in self.db.load("turnos") if x["estado"] == "abierto"]), 2)

    def test_sale_is_atomic_and_prevents_overselling(self):
        self.db.upsert_many("productos", [{"codigo":"P1", "nombre":"Pan", "precio":10, "stock":1}])
        self.db.open_shift(self.shift())
        outcomes = []
        barrier = threading.Barrier(2)
        def sell(invoice):
            barrier.wait()
            try:
                self.db.commit_sale(self.sale(invoice), [{"codigo":"P1", "cantidad":1}], invoice)
                outcomes.append("ok")
            except Exception:
                outcomes.append("error")
        a = threading.Thread(target=sell, args=("V-1",))
        b = threading.Thread(target=sell, args=("V-2",))
        a.start(); b.start(); a.join(); b.join()
        self.assertCountEqual(outcomes, ["ok", "error"])
        self.assertEqual(self.db.load("productos")[0]["stock"], 0)
        self.assertEqual(len(self.db.load("ventas")), 1)

    def test_cash_close_recalculates_inside_transaction(self):
        self.db.open_shift(self.shift())
        self.db.upsert_many("productos", [{"codigo":"P1", "nombre":"Pan", "precio":10, "stock":1}])
        self.db.commit_sale(self.sale("V-3"), [{"codigo":"P1", "cantidad":1}], "request-3")
        movement = {"id":"M-1", "turno_id":"T-1", "caja_id":"CAJA-01", "tipo":"Salida", "monto":10, "usuario":"ana"}
        self.db.add_cash_movement(movement)
        closed = self.db.close_shift("T-1", expected=0, counted=115, closed_at="ahora")
        self.assertEqual(closed["efectivo_esperado"], 115.0)
        self.assertEqual(closed["diferencia"], 0.0)

    def test_migration_keeps_json_source_and_verifies_counts(self):
        source = self.root / "legacy"; source.mkdir()
        docs = {
            "productos.json": [{"codigo":"A", "nombre":"A", "stock":4}],
            "ventas.json": [{"factura":"V1", "total":10}],
            "usuarios.json": [{"username":"admin", "role":"admin"}],
            "turnos_caja.json": [], "movimientos_caja.json": []
        }
        for name, data in docs.items(): (source / name).write_text(json.dumps(data), encoding="utf-8")
        (source / "actividades.jsonl").write_text(json.dumps({"fecha":"hoy", "usuario":"admin", "accion":"login", "detalle":"ok"}) + "\n", encoding="utf-8")
        before = {p.name:p.read_bytes() for p in source.iterdir()}
        manifest = self.db.migrate_json(source, self.root / "backups")
        self.assertEqual(manifest["integrity_check"], "ok")
        self.assertEqual(manifest["conteos_origen"]["productos"], 1)
        self.assertEqual(manifest["auditoria_origen"], 1)
        self.assertEqual(manifest["auditoria_sqlite"], 1)
        self.assertEqual(before, {p.name:p.read_bytes() for p in source.iterdir()})
        self.assertEqual(self.db.load("productos")[0]["codigo"], "A")
        self.assertTrue((Path(manifest["backup"]) / "manifest.json").exists())

    def test_request_id_blocks_repeated_sale(self):
        self.db.open_shift(self.shift())
        self.db.upsert_many("productos", [{"codigo":"P1", "nombre":"Pan", "precio":10, "stock":2}])
        self.db.commit_sale(self.sale("V-4"), [{"codigo":"P1", "cantidad":1}], "same-request")
        with self.assertRaises(ValueError):
            self.db.commit_sale(self.sale("V-5"), [{"codigo":"P1", "cantidad":1}], "same-request")
        self.assertEqual(len(self.db.load("ventas")), 1)

    def test_adjust_existing_product_updates_stock(self):
        self.db.upsert_many("productos", [{"codigo":"P1", "nombre":"Pan", "precio":10, "stock":2}])
        self.db.adjust_product({"codigo":"P1", "nombre":"Pan", "precio":12}, 3)
        product = self.db.load("productos")[0]
        self.assertEqual(product["stock"], 5)
        self.assertEqual(product["precio"], 12.0)

    def test_duplicate_product_lines_are_aggregated_before_stock_check(self):
        self.db.upsert_many("productos", [{"codigo":"P1", "nombre":"Pan", "precio":10, "stock":3}])
        self.db.open_shift(self.shift())
        with self.assertRaisesRegex(ValueError, "Stock insuficiente"):
            self.db.commit_sale(self.sale("V-DUP"),
                [{"codigo":"P1", "cantidad":2}, {"codigo":"P1", "cantidad":2}], "request-dup")
        self.assertEqual(self.db.load("productos")[0]["stock"], 3)
        self.assertEqual(self.db.load("ventas"), [])

    def test_cash_movement_rejects_zero_and_non_finite(self):
        self.db.open_shift(self.shift())
        for amount in (0, -1, float("nan"), float("inf")):
            movement = {"id":f"M-{amount}", "turno_id":"T-1", "caja_id":"CAJA-01",
                        "tipo":"Entrada", "monto":amount, "usuario":"ana"}
            with self.assertRaises(ValueError):
                self.db.add_cash_movement(movement)

    def test_migration_failure_does_not_partially_import(self):
        source = self.root / "bad_legacy"; source.mkdir()
        (source / "productos.json").write_text(json.dumps([{"codigo":"P1", "stock":1}]), encoding="utf-8")
        (source / "ventas.json").write_text("{bad json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.db.migrate_json(source, self.root / "backups")
        self.assertTrue(all(v == 0 for v in self.db.counts().values()))

    def test_sqlite_backup_is_consistent_and_independent(self):
        self.db.upsert_many("productos", [{"codigo":"P1", "nombre":"Pan", "precio":10, "stock":4}])
        backup = self.db.backup_to(self.root / "backup" / "pos_backup.db")
        copied = POSDatabase(backup)
        self.assertEqual(copied.integrity_check(), "ok")
        self.assertEqual(copied.load("productos")[0]["stock"], 4)
        self.db.adjust_product({"codigo":"P1", "nombre":"Pan", "precio":10}, 1)
        self.assertEqual(copied.load("productos")[0]["stock"], 4)
        self.assertEqual(self.db.load("productos")[0]["stock"], 5)

    def test_sale_failure_rolls_back_every_product(self):
        self.db.upsert_many("productos", [
            {"codigo":"P1", "nombre":"Pan", "precio":10, "stock":5},
            {"codigo":"P2", "nombre":"Leche", "precio":8, "stock":1},
        ])
        self.db.open_shift(self.shift())
        with self.assertRaisesRegex(ValueError, "Stock insuficiente"):
            self.db.commit_sale(self.sale("V-ROLLBACK"),
                [{"codigo":"P1", "cantidad":2}, {"codigo":"P2", "cantidad":2}], "request-rollback")
        stock = {p["codigo"]: p["stock"] for p in self.db.load("productos")}
        self.assertEqual(stock, {"P1":5, "P2":1})
        self.assertEqual(self.db.load("ventas"), [])

if __name__ == "__main__":
    unittest.main(verbosity=2)
