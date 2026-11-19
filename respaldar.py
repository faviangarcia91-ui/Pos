from __future__ import annotations
import argparse
from datetime import datetime
from pathlib import Path
from database import POSDatabase


def main() -> int:
    parser = argparse.ArgumentParser(description="Crear una copia consistente de la base SQLite del POS")
    parser.add_argument("--destino", default="respaldos/sqlite", help="Carpeta donde guardar el respaldo")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    db = POSDatabase(root / "datos" / "pos.db")
    folder = (root / args.destino).resolve()
    name = f"pos_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    destination = db.backup_to(folder / name)
    print("RESPALDO VERIFICADO")
    print(f"Archivo: {destination}")
    print(f"Integridad: {POSDatabase(destination).integrity_check()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
