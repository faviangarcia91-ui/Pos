from __future__ import annotations
import argparse, json
from pathlib import Path
from database import POSDatabase


def main():
    parser = argparse.ArgumentParser(description="Migrar datos JSON de POS 3.1 a SQLite POS 3.2")
    parser.add_argument("--origen", required=True, help="Carpeta datos/ del POS 3.1")
    parser.add_argument("--terminal", default="CAJA-01", help="Terminal asignada a los turnos abiertos importados")
    args = parser.parse_args()
    source = Path(args.origen).expanduser().resolve()
    if not source.is_dir(): raise SystemExit(f"No existe la carpeta origen: {source}")
    db = POSDatabase(Path(__file__).resolve().parent / "datos" / "pos.db")
    counts = db.counts()
    if any(counts.values()):
        raise SystemExit("La base SQLite ya contiene registros. No se importó para evitar mezclar o duplicar datos.")
    # Evitar importar varios turnos abiertos a una sola caja, lo que haría ambiguo el arqueo.
    shifts_file = source / "turnos_caja.json"
    if shifts_file.exists():
        shifts = json.loads(shifts_file.read_text(encoding="utf-8"))
        open_shifts = [s for s in shifts if s.get("estado") == "abierto"]
        if len(open_shifts) > 1:
            raise SystemExit("Hay varios turnos abiertos en POS 3.1. Cierra los turnos antes de migrar o asigna terminales distintas manualmente.")
    manifest = db.migrate_json(source, Path(__file__).resolve().parent / "respaldos", args.terminal.strip().upper())
    print("MIGRACIÓN VERIFICADA")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("Los archivos JSON originales no se modificaron ni se borraron.")

if __name__ == "__main__": main()
