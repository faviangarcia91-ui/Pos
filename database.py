"""Capa SQLite del POS 3.2. Solo usa la biblioteca estándar de Python."""
from __future__ import annotations
import json, sqlite3, shutil, math
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime

COLLECTIONS = {
    "productos": "productos", "ventas": "ventas", "usuarios": "usuarios",
    "turnos": "turnos_caja", "movimientos": "movimientos_caja"
}
KEY_FIELDS = {"productos": "codigo", "ventas": "factura", "usuarios": "username",
              "turnos": "id", "movimientos": "id"}


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class POSDatabase:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=20000")
        try:
            yield con
        finally:
            con.close()

    def _init_schema(self):
        with self.connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS productos(
                    record_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ventas(
                    record_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS usuarios(
                    record_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS turnos(
                    record_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS movimientos(
                    record_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT NOT NULL,
                    usuario TEXT NOT NULL, accion TEXT NOT NULL, detalle TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS terminales(
                    terminal_id TEXT PRIMARY KEY, nombre TEXT NOT NULL, activa INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS sales_guard(
                    request_id TEXT PRIMARY KEY, invoice TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_audit_fecha ON audit_log(fecha);
                CREATE INDEX IF NOT EXISTS idx_ventas_turno_metodo
                    ON ventas(json_extract(payload, '$.turno_id'), json_extract(payload, '$.metodo'));
                CREATE INDEX IF NOT EXISTS idx_ventas_cajero
                    ON ventas(json_extract(payload, '$.usuario'));
                CREATE INDEX IF NOT EXISTS idx_ventas_caja
                    ON ventas(json_extract(payload, '$.caja_id'));
                CREATE INDEX IF NOT EXISTS idx_movimientos_turno
                    ON movimientos(json_extract(payload, '$.turno_id'));
            """)

    def load(self, collection: str) -> list[dict]:
        if collection not in COLLECTIONS: raise ValueError("Colección desconocida")
        with self.connect() as con:
            rows = con.execute(f"SELECT payload FROM {collection} ORDER BY rowid").fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def upsert_many(self, collection: str, items: list[dict]):
        if collection not in COLLECTIONS: raise ValueError("Colección desconocida")
        key_field = KEY_FIELDS[collection]
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for item in items:
                key = str(item.get(key_field, "")).strip()
                if not key: raise ValueError(f"Registro sin clave {key_field}")
                con.execute(f"""INSERT INTO {collection}(record_key,payload,updated_at)
                    VALUES(?,?,?) ON CONFLICT(record_key)
                    DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at""",
                    (key, dumps(item), timestamp()))
            con.commit()

    def append_audit(self, username: str, action: str, details: str = ""):
        with self.connect() as con:
            con.execute("INSERT INTO audit_log(fecha,usuario,accion,detalle) VALUES(?,?,?,?)",
                        (timestamp(), username, action, details))

    def audit_rows(self, limit=500):
        with self.connect() as con:
            rows = con.execute("SELECT fecha,usuario,accion,detalle FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def register_terminal(self, terminal_id: str):
        terminal_id = terminal_id.strip().upper()
        if not terminal_id or len(terminal_id) > 32: raise ValueError("Identificador de caja no válido")
        with self.connect() as con:
            con.execute("INSERT INTO terminales(terminal_id,nombre) VALUES(?,?) ON CONFLICT(terminal_id) DO NOTHING", (terminal_id, terminal_id))
        return terminal_id

    def open_shift(self, shift: dict):
        terminal = str(shift.get("caja_id", "")).strip().upper()
        if not terminal:
            raise ValueError("La caja debe tener un identificador.")
        if not str(shift.get("id", "")).strip():
            raise ValueError("El turno debe tener un identificador.")
        shift = dict(shift)
        shift["caja_id"] = terminal
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            active = con.execute("SELECT payload FROM turnos ").fetchall()
            for row in active:
                old = json.loads(row["payload"])
                if old.get("estado") == "abierto" and old.get("caja_id") == terminal:
                    raise ValueError(f"La caja {terminal} ya tiene un turno abierto ({old.get('id')}).")
            key = str(shift["id"])
            con.execute("INSERT INTO turnos VALUES(?,?,?)", (key, dumps(shift), timestamp()))
            con.commit()

    def add_cash_movement(self, movement: dict):
        movement = dict(movement)
        amount = movement.get("monto")
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            raise ValueError("El monto del movimiento no es válido.")
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("El monto del movimiento debe ser mayor que cero y finito.")
        if movement.get("tipo") not in ("Entrada", "Salida"):
            raise ValueError("El tipo de movimiento debe ser Entrada o Salida.")
        movement["monto"] = round(amount, 2)
        if not str(movement.get("id", "")).strip():
            raise ValueError("El movimiento debe tener un identificador.")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM turnos WHERE record_key=?", (movement["turno_id"],)).fetchone()
            if not row: raise ValueError("El turno ya no existe.")
            shift = json.loads(row["payload"])
            if shift.get("estado") != "abierto": raise ValueError("El turno ya está cerrado.")
            if movement.get("caja_id") != shift.get("caja_id", "CAJA-01"):
                raise ValueError("El movimiento pertenece a otra caja.")
            if movement.get("tipo") == "Salida":
                expected = self._expected_cash_in_transaction(con, shift)
                if float(movement.get("monto", 0)) > expected:
                    raise ValueError("La salida supera el efectivo disponible según los movimientos registrados.")
            key = str(movement["id"])
            con.execute("INSERT INTO movimientos VALUES(?,?,?)", (key, dumps(movement), timestamp()))
            con.commit()

    def close_shift(self, shift_id: str, expected: float, counted: float, closed_at: str):
        try:
            counted = float(counted)
        except (TypeError, ValueError):
            raise ValueError("El efectivo contado no es válido.")
        if not math.isfinite(counted) or counted < 0:
            raise ValueError("El efectivo contado debe ser un número finito y no negativo.")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM turnos WHERE record_key=?", (shift_id,)).fetchone()
            if not row: raise ValueError("Turno no encontrado.")
            shift = json.loads(row["payload"])
            if shift.get("estado") != "abierto": raise ValueError("El turno ya fue cerrado en otra terminal.")
            expected = self._expected_cash_in_transaction(con, shift)
            shift.update({"estado":"cerrado", "fecha_cierre":closed_at,
                          "efectivo_esperado":round(expected,2), "efectivo_contado":round(counted,2),
                          "diferencia":round(counted-expected,2)})
            con.execute("UPDATE turnos SET payload=?,updated_at=? WHERE record_key=?",
                        (dumps(shift), timestamp(), shift_id))
            con.commit()
            return shift

    @staticmethod
    def _expected_cash_in_transaction(con, shift):
        shift_id = shift["id"]
        opening = float(shift.get("efectivo_inicial", 0))
        sales_rows = con.execute(
            "SELECT payload FROM ventas WHERE json_extract(payload, '$.turno_id')=? "
            "AND json_extract(payload, '$.metodo')='Efectivo'", (shift_id,)
        ).fetchall()
        cash_sales = sum(float(json.loads(r["payload"]).get("total", 0)) for r in sales_rows)
        movement_rows = con.execute(
            "SELECT payload FROM movimientos WHERE json_extract(payload, '$.turno_id')=?", (shift_id,)
        ).fetchall()
        net = 0.0
        for row in movement_rows:
            obj = json.loads(row["payload"])
            net += float(obj.get("monto", 0)) * (1 if obj.get("tipo") == "Entrada" else -1)
        return round(opening + cash_sales + net, 2)

    def adjust_product(self, product: dict, stock_delta: int):
        code = str(product.get("codigo", "")).strip()
        if not code: raise ValueError("Código de producto obligatorio")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM productos WHERE record_key=?", (code,)).fetchone()
            if row:
                current = json.loads(row["payload"])
                current["stock"] = int(current.get("stock", 0)) + int(stock_delta)
                current["nombre"] = product.get("nombre", current.get("nombre", ""))
                current["precio"] = float(product.get("precio", current.get("precio", 0)))
                if current["stock"] < 0: raise ValueError("El inventario no puede quedar negativo.")
                payload = current
            else:
                payload = dict(product)
                payload["stock"] = int(stock_delta)
                if payload["stock"] < 0: raise ValueError("El inventario no puede quedar negativo.")
            con.execute("""INSERT INTO productos(record_key,payload,updated_at) VALUES(?,?,?)
                ON CONFLICT(record_key) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at""",
                (code, dumps(payload), timestamp()))
            con.commit()

    def commit_sale(self, sale: dict, items: list[dict], request_id: str):
        """Confirma venta, descuento de stock y clave idempotente en una transacción."""
        sale = dict(sale)
        request_id = str(request_id or "").strip()
        invoice = str(sale.get("factura", "")).strip()
        if not request_id:
            raise ValueError("Falta el identificador único de la solicitud.")
        if not invoice:
            raise ValueError("La venta debe tener un número de factura.")
        if not sale.get("turno_id"):
            raise ValueError("La venta debe estar asociada a un turno.")
        if not items:
            raise ValueError("No se puede registrar una venta sin productos.")

        # Consolidar códigos repetidos antes de validar y descontar stock.
        quantities: dict[str, int] = {}
        for item in items:
            code = str(item.get("codigo", "")).strip()
            try:
                qty = int(item.get("cantidad", 0))
            except (TypeError, ValueError):
                raise ValueError(f"Cantidad no válida para el producto {code or '(sin código)' }.")
            if not code or qty <= 0:
                raise ValueError("Cada artículo debe tener código y cantidad positiva.")
            quantities[code] = quantities.get(code, 0) + qty

        try:
            total = float(sale.get("total", 0))
        except (TypeError, ValueError):
            raise ValueError("El total de la venta no es válido.")
        if not math.isfinite(total) or total < 0:
            raise ValueError("El total debe ser finito y no negativo.")

        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            prior = con.execute("SELECT invoice FROM sales_guard WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                raise ValueError(f"Esta operación ya fue procesada como {prior['invoice']}.")
            shift_row = con.execute("SELECT payload FROM turnos WHERE record_key=?", (sale["turno_id"],)).fetchone()
            if not shift_row:
                raise ValueError("El turno de caja no existe.")
            shift_data = json.loads(shift_row["payload"])
            if shift_data.get("estado") != "abierto":
                raise ValueError("El turno de caja ya no está abierto. Actualiza y vuelve a intentarlo.")
            if sale.get("caja_id") != shift_data.get("caja_id", "CAJA-01"):
                raise ValueError("La venta y el turno pertenecen a cajas distintas.")

            # Todas las existencias se validan antes de modificar una sola.
            products = {}
            for code, qty in quantities.items():
                row = con.execute("SELECT payload FROM productos WHERE record_key=?", (code,)).fetchone()
                if not row:
                    raise ValueError(f"Producto {code} ya no existe.")
                product = json.loads(row["payload"])
                stock = int(product.get("stock", 0))
                if stock < qty:
                    raise ValueError(f"Stock insuficiente para {product.get('nombre', code)}. Disponible: {stock}.")
                product["stock"] = stock - qty
                products[code] = product

            now = timestamp()
            for code, product in products.items():
                con.execute("UPDATE productos SET payload=?,updated_at=? WHERE record_key=?",
                            (dumps(product), now, code))
            con.execute("INSERT INTO ventas(record_key,payload,updated_at) VALUES(?,?,?)",
                        (invoice, dumps(sale), now))
            con.execute("INSERT INTO sales_guard(request_id,invoice,created_at) VALUES(?,?,?)",
                        (request_id, invoice, now))
            con.commit()

    def migrate_json(self, source_dir: Path, backup_root: Path, terminal_id: str = "CAJA-01"):
        """Migra JSON de forma atómica, respaldando y verificando el origen."""
        source_dir = Path(source_dir)
        backup_root = Path(backup_root)
        if not source_dir.is_dir():
            raise ValueError(f"No existe la carpeta de origen: {source_dir}")
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_dir = backup_root / f"backup_json_{stamp}"
        backup_dir.mkdir()
        files = {
            "productos": "productos.json", "ventas": "ventas.json", "usuarios": "usuarios.json",
            "turnos": "turnos_caja.json", "movimientos": "movimientos_caja.json"
        }
        loaded: dict[str, list[dict]] = {}
        counts_before: dict[str, int] = {}

        # Leer y respaldar todos los archivos antes de escribir en SQLite.
        for collection, filename in files.items():
            path = source_dir / filename
            if path.exists():
                shutil.copy2(path, backup_dir / filename)
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
                    raise ValueError(f"{filename}: se esperaba una lista de objetos JSON.")
            else:
                data = []
            if collection in ("turnos", "ventas"):
                for item in data:
                    item.setdefault("caja_id", terminal_id)
            loaded[collection] = data
            counts_before[collection] = len(data)

        audit_file = source_dir / "actividades.jsonl"
        audit_rows = []
        if audit_file.exists():
            shutil.copy2(audit_file, backup_dir / audit_file.name)
            with audit_file.open("r", encoding="utf-8") as f:
                for line_number, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"actividades.jsonl, línea {line_number}: {exc}") from exc
                    if not isinstance(obj, dict):
                        raise ValueError(f"actividades.jsonl, línea {line_number}: se esperaba un objeto.")
                    audit_rows.append(obj)

        # Importación única: ante cualquier error SQLite revierte todo el bloque.
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = {c: con.execute(f"SELECT COUNT(*) FROM {c}").fetchone()[0] for c in COLLECTIONS}
            existing["auditoria"] = con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            if any(existing.values()):
                raise RuntimeError("La base de datos no está vacía; se cancela la migración para evitar mezclar datos.")
            for collection, items in loaded.items():
                key_field = KEY_FIELDS[collection]
                seen = set()
                for item in items:
                    key = str(item.get(key_field, "")).strip()
                    if not key:
                        raise ValueError(f"Registro sin clave {key_field} en {files[collection]}.")
                    if key in seen:
                        raise ValueError(f"Clave duplicada {key!r} en {files[collection]}.")
                    seen.add(key)
                    con.execute(f"INSERT INTO {collection}(record_key,payload,updated_at) VALUES(?,?,?)",
                                (key, dumps(item), timestamp()))
            for obj in audit_rows:
                con.execute("INSERT INTO audit_log(fecha,usuario,accion,detalle) VALUES(?,?,?,?)",
                            (obj.get("fecha", timestamp()), obj.get("usuario", ""),
                             obj.get("accion", ""), obj.get("detalle", "")))
            counts_after = {c: con.execute(f"SELECT COUNT(*) FROM {c}").fetchone()[0] for c in COLLECTIONS}
            audit_after = con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            if counts_after != counts_before or audit_after != len(audit_rows):
                raise RuntimeError("Los conteos de origen y destino no coinciden; se revirtió la migración.")
            con.commit()

        integrity = self.integrity_check()
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity_check: {integrity}. Conserva el respaldo y no uses la base.")
        manifest = {"fecha": timestamp(), "conteos_origen": counts_before,
                    "conteos_sqlite": counts_after, "auditoria_origen": len(audit_rows),
                    "auditoria_sqlite": audit_after, "integrity_check": integrity,
                    "origen_conservado": True, "backup": str(backup_dir)}
        (backup_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    def backup_to(self, destination: Path | str) -> Path:
        """Crea una copia consistente incluso si la base está en modo WAL."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.resolve() == self.path.resolve():
            raise ValueError("El destino del respaldo no puede ser la base activa.")
        target = sqlite3.connect(destination)
        try:
            with self.connect() as source:
                source.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"El respaldo no superó integrity_check: {result}")
        finally:
            target.close()
        return destination

    def integrity_check(self):
        with self.connect() as con: return con.execute("PRAGMA integrity_check").fetchone()[0]

    def counts(self):
        with self.connect() as con:
            result = {c: con.execute(f"SELECT COUNT(*) FROM {c}").fetchone()[0] for c in COLLECTIONS}
            result["auditoria"] = con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            return result
