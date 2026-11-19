import importlib.util
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pos_main", PROJECT / "main.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class POS31Tests(unittest.TestCase):
    def test_hash_password(self):
        salt, digest = module.hash_password("ClaveSegura2026")
        self.assertTrue(module.verify_password("ClaveSegura2026", salt, digest))
        self.assertFalse(module.verify_password("OtraClave2026", salt, digest))

    def test_random_salts(self):
        salt1, hash1 = module.hash_password("ClaveSegura2026")
        salt2, hash2 = module.hash_password("ClaveSegura2026")
        self.assertNotEqual(salt1, salt2)
        self.assertNotEqual(hash1, hash2)

    def test_cash_expected_calculation(self):
        shift = {"id": "T-1", "efectivo_inicial": 100.0}
        sales = [
            {"turno_id": "T-1", "metodo": "Efectivo", "total": 50.0},
            {"turno_id": "T-1", "metodo": "Tarjeta", "total": 200.0},
        ]
        movements = [
            {"turno_id": "T-1", "tipo": "Entrada", "monto": 20.0},
            {"turno_id": "T-1", "tipo": "Salida", "monto": 15.0},
        ]
        self.assertEqual(module.calculate_expected_cash(shift, sales, movements), 155.0)

    def test_number_rejects_nan_and_infinity(self):
        for raw in ("nan", "inf", "-inf"):
            with self.assertRaises(ValueError):
                module.number(raw, "monto")

if __name__ == "__main__":
    unittest.main(verbosity=2)
