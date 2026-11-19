from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from pathlib import Path
from datetime import datetime
from typing import Any
from database import POSDatabase
import uuid

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "datos"
RECEIPTS_DIR = APP_DIR / "recibos"
PRODUCTS_FILE = DATA_DIR / "productos.json"
SALES_FILE = DATA_DIR / "ventas.json"
USERS_FILE = DATA_DIR / "usuarios.json"
AUDIT_FILE = DATA_DIR / "actividades.jsonl"
SHIFTS_FILE = DATA_DIR / "turnos_caja.json"
CASH_MOVEMENTS_FILE = DATA_DIR / "movimientos_caja.json"
DB_FILE = DATA_DIR / "pos.db"
TERMINAL_ID = os.environ.get("POS_TERMINAL_ID", "CAJA-01").strip().upper() or "CAJA-01"
DB = POSDatabase(DB_FILE)

IVA_PREDETERMINADO = 19.0
PBKDF2_ITERATIONS = 600_000
ROLES = ("admin", "cajero")
PERMISSIONS = {
    "ventas": "Realizar ventas",
    "inventario_ver": "Consultar inventario",
    "inventario_gestionar": "Registrar y reponer productos",
    "reportes": "Consultar reportes",
    "usuarios": "Administrar usuarios",
    "caja_turnos": "Abrir, cerrar y consultar turnos de caja",
    "caja_movimientos": "Registrar entradas y salidas de efectivo",
}
ROLE_DEFAULTS = {
    "admin": list(PERMISSIONS),
    "cajero": ["ventas", "inventario_ver", "caja_turnos", "caja_movimientos"],
}


def now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def ensure_files() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    DB.register_terminal(TERMINAL_ID)


def load_json(path: Path, default: Any) -> Any:
    mapping = {PRODUCTS_FILE: "productos", SALES_FILE: "ventas", USERS_FILE: "usuarios",
               SHIFTS_FILE: "turnos", CASH_MOVEMENTS_FILE: "movimientos"}
    collection = mapping.get(path)
    if collection:
        return DB.load(collection)
    return default


def save_json(path: Path, data: Any) -> None:
    mapping = {PRODUCTS_FILE: "productos", SALES_FILE: "ventas", USERS_FILE: "usuarios",
               SHIFTS_FILE: "turnos", CASH_MOVEMENTS_FILE: "movimientos"}
    collection = mapping.get(path)
    if not collection:
        raise ValueError(f"No se reconoce la colección: {path.name}")
    DB.upsert_many(collection, data)


def audit(username: str, action: str, details: str = "") -> None:
    DB.append_audit(username, action, details)


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, expected_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()
        return hmac.compare_digest(actual, expected_hex)
    except (ValueError, TypeError):
        return False


def valid_password(password: str) -> bool:
    return len(password) >= 10 and any(c.isalpha() for c in password) and any(c.isdigit() for c in password)


def money(value: float) -> str:
    return f"${value:,.2f}"


def number(value: str, field: str) -> float:
    try:
        result = float(value.replace(",", ".").strip())
        if not math.isfinite(result) or result < 0:
            raise ValueError
        return result
    except ValueError:
        raise ValueError(f"El campo {field} debe ser un número válido y no negativo.")


def integer(value: str, field: str) -> int:
    try:
        result = int(value.strip())
        if result < 0:
            raise ValueError
        return result
    except ValueError:
        raise ValueError(f"El campo {field} debe ser un entero no negativo.")


def calculate_expected_cash(shift: dict, sales: list[dict], movements: list[dict]) -> float:
    """Calcula efectivo esperado: apertura + ventas en efectivo + entradas - salidas."""
    shift_id = shift.get("id")
    opening = float(shift.get("efectivo_inicial", 0))
    cash_sales = sum(
        float(s.get("total", 0)) for s in sales
        if s.get("turno_id") == shift_id and s.get("metodo") == "Efectivo"
    )
    cash_movements = sum(
        float(m.get("monto", 0)) if m.get("tipo") == "Entrada" else -float(m.get("monto", 0))
        for m in movements if m.get("turno_id") == shift_id
    )
    return round(opening + cash_sales + cash_movements, 2)


class UserStore:
    def __init__(self):
        self.users: list[dict] = load_json(USERS_FILE, [])
        if not isinstance(self.users, list):
            raise RuntimeError("usuarios.json debe contener una lista JSON válida.")
        # Migración compatible: POS 3.0 no tenía permisos para turnos ni movimientos.
        changed = False
        for user in self.users:
            permissions = user.get("permissions")
            if not isinstance(permissions, list):
                permissions = ROLE_DEFAULTS.get(user.get("role"), []).copy()
                user["permissions"] = permissions
                changed = True
            for new_permission in ("caja_turnos", "caja_movimientos"):
                if new_permission not in permissions:
                    permissions.append(new_permission)
                    changed = True
        if changed:
            save_json(USERS_FILE, self.users)

    def save(self):
        save_json(USERS_FILE, self.users)

    def get(self, username: str):
        return next((u for u in self.users if u.get("username", "").lower() == username.lower()), None)

    def create(self, username: str, password: str, role: str):
        username = username.strip()
        if len(username) < 3 or any(c in username for c in "|/\\"):
            raise ValueError("El usuario debe tener al menos 3 caracteres y no contener / ni barras.")
        if self.get(username):
            raise ValueError("Ese nombre de usuario ya existe.")
        if role not in ROLES:
            raise ValueError("Rol no válido.")
        if not valid_password(password):
            raise ValueError("La contraseña debe tener al menos 10 caracteres e incluir letras y números.")
        salt, digest = hash_password(password)
        self.users.append({
            "username": username, "role": role, "salt": salt,
            "password_hash": digest, "iterations": PBKDF2_ITERATIONS,
            "active": True, "created_at": now_text(),
            "permissions": ROLE_DEFAULTS[role].copy()
        })
        self.save()

    def authenticate(self, username: str, password: str):
        user = self.get(username)
        if not user or not user.get("active", True):
            return None
        if verify_password(password, user.get("salt", ""), user.get("password_hash", "")):
            return user
        return None

    def change_password(self, user: dict, new_password: str):
        if not valid_password(new_password):
            raise ValueError("Use al menos 10 caracteres, con letras y números.")
        salt, digest = hash_password(new_password)
        user["salt"], user["password_hash"] = salt, digest
        user["iterations"] = PBKDF2_ITERATIONS
        self.save()

    def set_role(self, user: dict, role: str):
        if role not in ROLES:
            raise ValueError("Rol no válido.")
        user["role"] = role
        user["permissions"] = ROLE_DEFAULTS[role].copy()
        self.save()

    def set_active(self, user: dict, active: bool):
        user["active"] = active
        self.save()


class POSApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Caja Registradora POS 3.2.1 — {TERMINAL_ID}")
        self.geometry("1120x720")
        self.minsize(950, 620)
        self.configure(padx=10, pady=10)
        ensure_files()
        self.store = UserStore()
        self.products = load_json(PRODUCTS_FILE, [])
        self.sales = load_json(SALES_FILE, [])
        self.shifts = load_json(SHIFTS_FILE, [])
        self.cash_movements = load_json(CASH_MOVEMENTS_FILE, [])
        self.cart: list[dict] = []
        self.pending_sale_request_id: str | None = None
        self.current_user: dict | None = None
        self.iva_var = tk.StringVar(value=str(IVA_PREDETERMINADO))
        self.discount_var = tk.StringVar(value="0")
        self._style()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        if not self.store.users:
            self.after(100, self.first_admin_setup)
        else:
            self.after(100, self.login_dialog)

    def _style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=25)
        style.configure("TButton", padding=6)

    def first_admin_setup(self):
        win = tk.Toplevel(self)
        win.title("Configuración inicial - Administrador")
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)
        ttk.Label(win, text="Crear cuenta inicial de administrador", font=("Arial", 12, "bold")).grid(row=0, column=0, columnspan=2, padx=14, pady=12)
        ttk.Label(win, text="Usuario").grid(row=1, column=0, sticky="w", padx=12, pady=5)
        username = ttk.Entry(win, width=28)
        username.grid(row=1, column=1, padx=12, pady=5)
        ttk.Label(win, text="Contraseña").grid(row=2, column=0, sticky="w", padx=12, pady=5)
        password = ttk.Entry(win, width=28, show="•")
        password.grid(row=2, column=1, padx=12, pady=5)
        ttk.Label(win, text="Confirmar").grid(row=3, column=0, sticky="w", padx=12, pady=5)
        confirm = ttk.Entry(win, width=28, show="•")
        confirm.grid(row=3, column=1, padx=12, pady=5)
        ttk.Label(win, text="Mínimo 10 caracteres, con letras y números.").grid(row=4, column=0, columnspan=2, padx=12, pady=5)
        def create():
            try:
                if password.get() != confirm.get():
                    raise ValueError("Las contraseñas no coinciden.")
                self.store.create(username.get(), password.get(), "admin")
                audit(username.get(), "CREAR_ADMIN_INICIAL")
                win.grab_release()
                win.destroy()
                self.login_dialog()
            except Exception as e:
                messagebox.showerror("No se pudo crear", str(e), parent=win)
        ttk.Button(win, text="Crear administrador", command=create).grid(row=5, column=0, columnspan=2, pady=12)
        win.protocol("WM_DELETE_WINDOW", self.destroy)

    def login_dialog(self):
        win = tk.Toplevel(self)
        win.title("Inicio de sesión")
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)
        ttk.Label(win, text="POS 3.2.1", font=("Arial", 18, "bold")).grid(row=0, column=0, columnspan=2, padx=20, pady=(18, 10))
        ttk.Label(win, text="Usuario").grid(row=1, column=0, sticky="w", padx=14, pady=6)
        user_entry = ttk.Entry(win, width=28)
        user_entry.grid(row=1, column=1, padx=14, pady=6)
        ttk.Label(win, text="Contraseña").grid(row=2, column=0, sticky="w", padx=14, pady=6)
        pass_entry = ttk.Entry(win, width=28, show="•")
        pass_entry.grid(row=2, column=1, padx=14, pady=6)
        attempts = {"count": 0}
        def login(event=None):
            user = self.store.authenticate(user_entry.get(), pass_entry.get())
            if user:
                self.current_user = user
                audit(user["username"], "INICIO_SESION")
                win.grab_release()
                win.destroy()
                self.build_ui()
            else:
                attempts["count"] += 1
                audit(user_entry.get().strip() or "desconocido", "FALLO_INICIO_SESION")
                pass_entry.delete(0, tk.END)
                messagebox.showerror("Acceso denegado", f"Credenciales incorrectas o usuario inactivo. Intento {attempts['count']}/5.", parent=win)
                if attempts["count"] >= 5:
                    self.destroy()
        ttk.Button(win, text="Ingresar", command=login).grid(row=3, column=0, columnspan=2, pady=14)
        pass_entry.bind("<Return>", login)
        user_entry.focus_set()
        win.protocol("WM_DELETE_WINDOW", self.destroy)

    def allowed(self, permission: str) -> bool:
        if not self.current_user:
            return False
        return permission in self.current_user.get("permissions", ROLE_DEFAULTS.get(self.current_user.get("role"), []))

    def require(self, permission: str) -> bool:
        if not self.allowed(permission):
            messagebox.showwarning("Permiso insuficiente", "Tu usuario no tiene permiso para esta función.")
            audit(self.current_user["username"], "ACCESO_DENEGADO", permission)
            return False
        return True

    def build_ui(self):
        for child in self.winfo_children():
            if isinstance(child, (ttk.Notebook, ttk.Frame, tk.Frame)):
                child.destroy()
        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="POS 3.2.1", font=("Arial", 18, "bold")).pack(side="left")
        ttk.Label(header, text=f"Sesión: {self.current_user['username']} | Rol: {self.current_user['role']}").pack(side="left", padx=18)
        ttk.Button(header, text="Cambiar contraseña", command=self.change_own_password).pack(side="right", padx=4)
        ttk.Button(header, text="Cerrar sesión", command=self.logout).pack(side="right", padx=4)
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        if self.allowed("ventas"):
            self.sales_tab = ttk.Frame(self.tabs, padding=10)
            self.tabs.add(self.sales_tab, text="Caja / Ventas")
            self.build_sales_tab()
        if self.allowed("inventario_ver"):
            self.inventory_tab = ttk.Frame(self.tabs, padding=10)
            self.tabs.add(self.inventory_tab, text="Inventario")
            self.build_inventory_tab()
        if self.allowed("caja_turnos"):
            self.cash_tab = ttk.Frame(self.tabs, padding=10)
            self.tabs.add(self.cash_tab, text="Turnos y caja")
            self.build_cash_tab()
        if self.allowed("reportes"):
            self.reports_tab = ttk.Frame(self.tabs, padding=10)
            self.tabs.add(self.reports_tab, text="Reportes")
            self.build_reports_tab()
        if self.allowed("usuarios"):
            self.users_tab = ttk.Frame(self.tabs, padding=10)
            self.tabs.add(self.users_tab, text="Usuarios")
            self.build_users_tab()

    def current_open_shift(self):
        if not self.current_user:
            return None
        return next((s for s in reversed(self.shifts)
                     if s.get("caja_id", "CAJA-01") == TERMINAL_ID
                     and s.get("usuario") == self.current_user["username"]
                     and s.get("estado") == "abierto"), None)

    def shift_cash_expected(self, shift: dict) -> float:
        return calculate_expected_cash(shift, self.sales, self.cash_movements)

    def build_cash_tab(self):
        top = ttk.LabelFrame(self.cash_tab, text="Estado del turno", padding=10)
        top.pack(fill="x", pady=(0, 8))
        self.shift_status = ttk.Label(top, text="Sin turno abierto", font=("Arial", 12, "bold"))
        self.shift_status.pack(side="left", padx=5)
        ttk.Button(top, text="Abrir turno", command=self.open_cash_shift).pack(side="right", padx=4)
        ttk.Button(top, text="Cerrar turno / arqueo", command=self.close_cash_shift).pack(side="right", padx=4)

        movement_box = ttk.LabelFrame(self.cash_tab, text="Movimientos de efectivo", padding=8)
        movement_box.pack(fill="x", pady=6)
        self.cash_movement_type = tk.StringVar(value="Entrada")
        ttk.Combobox(movement_box, textvariable=self.cash_movement_type,
                     values=("Entrada", "Salida"), state="readonly", width=10).pack(side="left", padx=4)
        self.cash_movement_amount = ttk.Entry(movement_box, width=14)
        self.cash_movement_amount.pack(side="left", padx=4)
        self.cash_movement_reason = ttk.Entry(movement_box, width=38)
        self.cash_movement_reason.pack(side="left", padx=4)
        ttk.Label(movement_box, text="Monto y concepto").pack(side="left", padx=4)
        ttk.Button(movement_box, text="Registrar movimiento", command=self.add_cash_movement).pack(side="left", padx=4)

        columns = ("fecha", "usuario", "estado", "inicial", "esperado", "contado", "diferencia")
        self.shifts_tree = ttk.Treeview(self.cash_tab, columns=columns, show="headings", height=11)
        for key, title, width in (("fecha", "Apertura", 165), ("usuario", "Cajero", 120),
                                  ("estado", "Estado", 90), ("inicial", "Inicial", 105),
                                  ("esperado", "Esperado", 105), ("contado", "Contado", 105),
                                  ("diferencia", "Diferencia", 105)):
            self.shifts_tree.heading(key, text=title)
            self.shifts_tree.column(key, width=width, anchor="center")
        self.shifts_tree.pack(fill="both", expand=True, pady=8)
        self.refresh_cash_tab()

    def refresh_cash_tab(self):
        if not hasattr(self, "shifts_tree"):
            return
        self.shifts = DB.load("turnos")
        self.sales = DB.load("ventas")
        self.cash_movements = DB.load("movimientos")
        shift = self.current_open_shift()
        if shift:
            expected = self.shift_cash_expected(shift)
            self.shift_status.config(text=f"Turno ABIERTO | Inicio: {shift.get('fecha_apertura')} | Efectivo esperado: {money(expected)}")
        else:
            self.shift_status.config(text="No tienes un turno abierto. Abre caja antes de vender.")
        for iid in self.shifts_tree.get_children():
            self.shifts_tree.delete(iid)
        visible = [s for s in self.shifts if s.get("caja_id", "CAJA-01") == TERMINAL_ID]
        if not self.allowed("usuarios"):
            visible = [s for s in visible if s.get("usuario") == self.current_user["username"]]
        for index, shift_row in enumerate(reversed(visible)):
            expected = self.shift_cash_expected(shift_row)
            counted = shift_row.get("efectivo_contado", "—")
            difference = shift_row.get("diferencia", "—")
            self.shifts_tree.insert("", "end", iid=str(index), values=(
                shift_row.get("fecha_apertura", ""), shift_row.get("usuario", ""),
                shift_row.get("estado", ""), money(float(shift_row.get("efectivo_inicial", 0))),
                money(expected), money(float(counted)) if isinstance(counted, (int, float)) else counted,
                money(float(difference)) if isinstance(difference, (int, float)) else difference,
            ))

    def open_cash_shift(self):
        if not self.require("caja_turnos"):
            return
        if self.current_open_shift():
            return messagebox.showwarning("Caja", "Ya tienes un turno abierto.")
        raw = simpledialog.askstring("Apertura de caja", "Efectivo inicial en caja:", parent=self)
        if raw is None:
            return
        try:
            opening = number(raw, "efectivo inicial")
        except ValueError as exc:
            return messagebox.showerror("Apertura", str(exc))
        shift = {"id": "T-" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2).upper(),
                 "usuario": self.current_user["username"], "fecha_apertura": now_text(),
                 "efectivo_inicial": round(opening, 2), "estado": "abierto", "caja_id": TERMINAL_ID,
                 "fecha_cierre": None, "efectivo_contado": None, "diferencia": None}
        try:
            DB.open_shift(shift)
            self.shifts = DB.load("turnos")
        except Exception as exc:
            return messagebox.showerror("Apertura", f"No se pudo abrir el turno: {exc}")
        audit(self.current_user["username"], "APERTURA_TURNO", f"{shift['id']} | {money(opening)}")
        self.refresh_cash_tab()

    def add_cash_movement(self):
        if not self.require("caja_movimientos"):
            return
        shift = self.current_open_shift()
        if not shift:
            return messagebox.showwarning("Caja", "Debes abrir un turno antes de registrar movimientos.")
        try:
            amount = number(self.cash_movement_amount.get(), "monto")
            if amount <= 0:
                raise ValueError("El monto debe ser mayor que cero.")
            reason = self.cash_movement_reason.get().strip()
            if len(reason) < 3:
                raise ValueError("Escribe un concepto de al menos 3 caracteres.")
            kind = self.cash_movement_type.get()
            if kind == "Salida" and amount > self.shift_cash_expected(shift):
                raise ValueError("La salida supera el efectivo esperado en caja.")
        except ValueError as exc:
            return messagebox.showerror("Movimiento", str(exc))
        movement = {"id": "M-" + secrets.token_hex(5).upper(), "turno_id": shift["id"],
                    "caja_id": TERMINAL_ID, "usuario": self.current_user["username"], "fecha": now_text(),
                    "tipo": kind, "monto": round(amount, 2), "concepto": reason}
        movement["caja_id"] = TERMINAL_ID
        try:
            DB.add_cash_movement(movement)
            self.cash_movements = DB.load("movimientos")
        except Exception as exc:
            return messagebox.showerror("Movimiento", f"No se pudo guardar: {exc}")
        audit(self.current_user["username"], "MOVIMIENTO_CAJA_" + kind.upper(), f"{money(amount)} | {reason}")
        self.cash_movement_amount.delete(0, tk.END)
        self.cash_movement_reason.delete(0, tk.END)
        self.refresh_cash_tab()

    def close_cash_shift(self):
        if not self.require("caja_turnos"):
            return
        shift = self.current_open_shift()
        if not shift:
            return messagebox.showwarning("Caja", "No tienes un turno abierto.")
        expected = self.shift_cash_expected(shift)
        raw = simpledialog.askstring("Cierre y arqueo", f"Efectivo esperado: {money(expected)}\nCuenta el dinero físico e ingresa el total:", parent=self)
        if raw is None:
            return
        try:
            counted = number(raw, "efectivo contado")
        except ValueError as exc:
            return messagebox.showerror("Cierre", str(exc))
        difference = round(counted - expected, 2)
        if not messagebox.askyesno("Confirmar cierre", f"Esperado: {money(expected)}\nContado: {money(counted)}\nDiferencia: {money(difference)}\n\n¿Cerrar el turno?", parent=self):
            return
        try:
            closed = DB.close_shift(shift["id"], expected, counted, now_text())
            expected = float(closed["efectivo_esperado"])
            difference = float(closed["diferencia"])
            self.shifts = DB.load("turnos")
        except Exception as exc:
            return messagebox.showerror("Cierre", f"No se pudo cerrar el turno: {exc}")
        audit(self.current_user["username"], "CIERRE_TURNO", f"{shift['id']} | diferencia {money(difference)}")
        self.refresh_cash_tab()
        messagebox.showinfo("Turno cerrado", f"Efectivo esperado: {money(expected)}\nEfectivo contado: {money(counted)}\nDiferencia: {money(difference)}")

    def build_sales_tab(self):
        top = ttk.Frame(self.sales_tab); top.pack(fill="x")
        ttk.Label(top, text="Código de producto").pack(side="left")
        self.sale_code = ttk.Entry(top, width=22); self.sale_code.pack(side="left", padx=6)
        ttk.Button(top, text="Agregar", command=self.add_to_cart).pack(side="left")
        self.sale_code.bind("<Return>", lambda e: self.add_to_cart())
        cols = ("codigo", "nombre", "cantidad", "precio", "total")
        self.cart_tree = ttk.Treeview(self.sales_tab, columns=cols, show="headings", height=13)
        for c, title, width in zip(cols, ("Código", "Producto", "Cant.", "Precio", "Total"), (130, 330, 80, 120, 130)):
            self.cart_tree.heading(c, text=title); self.cart_tree.column(c, width=width, anchor="center")
        self.cart_tree.pack(fill="both", expand=True, pady=10)
        actions = ttk.Frame(self.sales_tab); actions.pack(fill="x")
        ttk.Button(actions, text="Quitar seleccionado", command=self.remove_cart_item).pack(side="left", padx=3)
        ttk.Button(actions, text="Vaciar carrito", command=self.clear_cart).pack(side="left", padx=3)
        ttk.Label(actions, text="IVA %").pack(side="left", padx=(18, 3))
        ttk.Entry(actions, textvariable=self.iva_var, width=6).pack(side="left")
        ttk.Label(actions, text="Descuento %").pack(side="left", padx=(12, 3))
        ttk.Entry(actions, textvariable=self.discount_var, width=6).pack(side="left")
        ttk.Button(actions, text="Cobrar", command=self.checkout).pack(side="right", padx=4)
        self.total_label = ttk.Label(self.sales_tab, text="Total: $0.00", font=("Arial", 15, "bold"))
        self.total_label.pack(anchor="e", pady=8)

    def add_to_cart(self):
        code = self.sale_code.get().strip()
        product = next((p for p in self.products if str(p.get("codigo", "")).lower() == code.lower()), None)
        if not product:
            messagebox.showerror("Producto", "No se encontró el código.")
            return
        if int(product.get("stock", 0)) <= 0:
            messagebox.showwarning("Inventario", "Producto sin existencias.")
            return
        item = next((x for x in self.cart if x["codigo"] == product["codigo"]), None)
        if item:
            if item["cantidad"] + 1 > int(product.get("stock", 0)):
                messagebox.showwarning("Inventario", "No hay stock suficiente.")
                return
            item["cantidad"] += 1
        else:
            self.cart.append({"codigo": product["codigo"], "nombre": product["nombre"], "cantidad": 1, "precio": float(product["precio"])})
        self.sale_code.delete(0, tk.END)
        self.refresh_cart()

    def refresh_cart(self):
        for i in self.cart_tree.get_children(): self.cart_tree.delete(i)
        subtotal = 0.0
        for item in self.cart:
            total = item["cantidad"] * item["precio"]; subtotal += total
            self.cart_tree.insert("", "end", iid=item["codigo"], values=(item["codigo"], item["nombre"], item["cantidad"], money(item["precio"]), money(total)))
        try:
            discount = number(self.discount_var.get(), "descuento")
            iva = number(self.iva_var.get(), "IVA")
            if discount > 100 or iva > 100: raise ValueError()
            discounted = subtotal * (1 - discount / 100)
            total = discounted * (1 + iva / 100)
            self.total_label.config(text=f"Total: {money(total)}")
        except ValueError:
            self.total_label.config(text="Revisa IVA y descuento")

    def remove_cart_item(self):
        selected = self.cart_tree.selection()
        if not selected: return
        self.cart = [x for x in self.cart if x["codigo"] != selected[0]]
        self.refresh_cart()

    def clear_cart(self):
        self.cart.clear(); self.refresh_cart()

    def checkout(self):
        if not self.require("ventas"):
            return
        active_shift = self.current_open_shift()
        if not active_shift:
            return messagebox.showwarning("Caja", "Debes abrir un turno en la pestaña Turnos y caja antes de cobrar.")
        if not self.cart: return messagebox.showwarning("Caja", "El carrito está vacío.")
        try:
            discount_pct = number(self.discount_var.get(), "descuento")
            iva_pct = number(self.iva_var.get(), "IVA")
            if discount_pct > 100 or iva_pct > 100: raise ValueError("IVA y descuento no pueden superar 100%.")
        except ValueError as e:
            return messagebox.showerror("Datos", str(e))
        for item in self.cart:
            product = next((p for p in self.products if p["codigo"] == item["codigo"]), None)
            if not product or int(product.get("stock", 0)) < item["cantidad"]:
                return messagebox.showerror("Inventario", f"Stock insuficiente para {item['nombre']}.")
        subtotal = sum(x["cantidad"] * x["precio"] for x in self.cart)
        discount = subtotal * discount_pct / 100
        base = subtotal - discount
        tax = base * iva_pct / 100
        total = round(base + tax, 2)
        method = simpledialog.askstring("Pago", f"Total a pagar: {money(total)}\nMétodo (Efectivo, Tarjeta, Transferencia):", parent=self)
        if not method: return
        method = method.strip().capitalize()
        if method not in ("Efectivo", "Tarjeta", "Transferencia"):
            return messagebox.showerror("Pago", "Método no válido.")
        received, change = total, 0.0
        if method == "Efectivo":
            raw = simpledialog.askstring("Pago en efectivo", f"Total: {money(total)}\nDinero recibido:", parent=self)
            if raw is None: return
            try: received = number(raw, "dinero recibido")
            except ValueError as e: return messagebox.showerror("Pago", str(e))
            if received < total: return messagebox.showerror("Pago", "El dinero recibido es insuficiente.")
            change = round(received - total, 2)
        invoice = f"V-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"
        sale = {
            "factura": invoice, "fecha": now_text(), "usuario": self.current_user["username"],
            "turno_id": active_shift["id"], "caja_id": TERMINAL_ID, "productos": [dict(x) for x in self.cart], "subtotal": round(subtotal, 2),
            "descuento": round(discount, 2), "iva": round(tax, 2), "total": total,
            "metodo": method, "recibido": round(received, 2), "cambio": change,
            "porcentaje_descuento": discount_pct, "porcentaje_iva": iva_pct
        }
        receipt = RECEIPTS_DIR / f"{invoice}.txt"
        receipt.write_text(
            "COMPROBANTE DE VENTA\n" + f"Factura: {invoice}\nFecha: {sale['fecha']}\nCaja: {TERMINAL_ID}\nCajero: {sale['usuario']}\n" +
            "\n".join(f"{x['nombre']} x{x['cantidad']}  {money(x['precio'] * x['cantidad'])}" for x in self.cart) +
            f"\n\nSubtotal: {money(subtotal)}\nDescuento: {money(discount)}\nIVA: {money(tax)}\nTOTAL: {money(total)}\nPago: {method}\nRecibido: {money(received)}\nCambio: {money(change)}\n",
            encoding="utf-8"
        )
        if self.pending_sale_request_id is None:
            self.pending_sale_request_id = str(uuid.uuid4())
        try:
            DB.commit_sale(sale, self.cart, self.pending_sale_request_id)
        except Exception as e:
            try: receipt.unlink(missing_ok=True)
            except Exception: pass
            return messagebox.showerror("Venta no registrada", f"La transacción fue cancelada; no se descontó inventario.\n{e}")
        self.pending_sale_request_id = None
        self.products = DB.load("productos")
        self.sales = DB.load("ventas")
        audit(self.current_user["username"], "VENTA_REGISTRADA", f"{invoice} | {money(total)}")
        messagebox.showinfo("Venta completada", f"Factura: {invoice}\nTotal: {money(total)}\nCambio: {money(change)}\nComprobante: {receipt.name}")
        self.cart.clear(); self.refresh_cart()
        if hasattr(self, "inventory_tree"): self.refresh_inventory()
        if hasattr(self, "reports_tree"): self.refresh_reports()
        if hasattr(self, "shifts_tree"): self.refresh_cash_tab()

    def build_inventory_tab(self):
        if not self.allowed("inventario_ver"): return
        cols = ("codigo", "nombre", "precio", "stock")
        self.inventory_tree = ttk.Treeview(self.inventory_tab, columns=cols, show="headings", height=14)
        for c, title, width in zip(cols, ("Código", "Producto", "Precio", "Existencias"), (150, 400, 150, 120)):
            self.inventory_tree.heading(c, text=title); self.inventory_tree.column(c, width=width)
        self.inventory_tree.pack(fill="both", expand=True)
        self.refresh_inventory()
        if self.allowed("inventario_gestionar"):
            form = ttk.LabelFrame(self.inventory_tab, text="Registrar producto / ingresar mercancía", padding=8)
            form.pack(fill="x", pady=10)
            self.inv_code = ttk.Entry(form, width=16); self.inv_name = ttk.Entry(form, width=24)
            self.inv_price = ttk.Entry(form, width=12); self.inv_stock = ttk.Entry(form, width=8)
            for label, widget in (("Código", self.inv_code), ("Nombre", self.inv_name), ("Precio", self.inv_price), ("Stock inicial / ingreso", self.inv_stock)):
                ttk.Label(form, text=label).pack(side="left", padx=(5, 2)); widget.pack(side="left", padx=3)
            ttk.Button(form, text="Guardar / reponer", command=self.save_product).pack(side="left", padx=6)

    def refresh_inventory(self):
        if not hasattr(self, "inventory_tree"): return
        self.products = DB.load("productos")
        for i in self.inventory_tree.get_children(): self.inventory_tree.delete(i)
        for p in self.products:
            self.inventory_tree.insert("", "end", values=(p.get("codigo"), p.get("nombre"), money(float(p.get("precio", 0))), p.get("stock", 0)))

    def save_product(self):
        if not self.require("inventario_gestionar"): return
        code, name = self.inv_code.get().strip(), self.inv_name.get().strip()
        if not code or not name: return messagebox.showerror("Inventario", "Código y nombre son obligatorios.")
        try:
            price = number(self.inv_price.get(), "precio")
            qty = integer(self.inv_stock.get(), "stock")
        except ValueError as e: return messagebox.showerror("Datos", str(e))
        product = next((p for p in DB.load("productos") if str(p.get("codigo", "")).lower() == code.lower()), None)
        if product:
            action = "REPOSICION_INVENTARIO"
        else:
            action = "PRODUCTO_CREADO"
        try:
            DB.adjust_product({"codigo": code, "nombre": name, "precio": price}, qty)
            self.products = DB.load("productos")
        except Exception as exc:
            return messagebox.showerror("Inventario", str(exc))
        audit(self.current_user["username"], action, code)
        self.refresh_inventory()
        for w in (self.inv_code, self.inv_name, self.inv_price, self.inv_stock): w.delete(0, tk.END)
        messagebox.showinfo("Inventario", "Inventario guardado.")

    def build_reports_tab(self):
        cols = ("fecha", "factura", "caja", "usuario", "metodo", "total")
        self.reports_tree = ttk.Treeview(self.reports_tab, columns=cols, show="headings", height=16)
        for c, title, width in zip(cols, ("Fecha", "Factura", "Caja", "Usuario", "Pago", "Total"), (170, 200, 100, 120, 110, 120)):
            self.reports_tree.heading(c, text=title); self.reports_tree.column(c, width=width)
        self.reports_tree.pack(fill="both", expand=True)
        self.report_total = ttk.Label(self.reports_tab, font=("Arial", 13, "bold"))
        self.report_total.pack(anchor="e", pady=6)
        ttk.Label(self.reports_tab, text="Resumen de ventas por cajero", font=("Arial", 11, "bold")).pack(anchor="w", pady=(8, 2))
        self.cashier_summary = ttk.Treeview(self.reports_tab, columns=("usuario", "cantidad", "total"), show="headings", height=5)
        for key, title, width in (("usuario", "Cajero", 220), ("cantidad", "N.º ventas", 120), ("total", "Total vendido", 180)):
            self.cashier_summary.heading(key, text=title); self.cashier_summary.column(key, width=width)
        self.cashier_summary.pack(fill="x")
        ttk.Label(self.reports_tab, text="Resumen de ventas por caja", font=("Arial", 11, "bold")).pack(anchor="w", pady=(8, 2))
        self.terminal_summary = ttk.Treeview(self.reports_tab, columns=("caja", "cantidad", "total"), show="headings", height=4)
        for key, title, width in (("caja", "Caja", 220), ("cantidad", "N.º ventas", 120), ("total", "Total vendido", 180)):
            self.terminal_summary.heading(key, text=title); self.terminal_summary.column(key, width=width)
        self.terminal_summary.pack(fill="x")
        self.refresh_reports()

    def refresh_reports(self):
        if not hasattr(self, "reports_tree"): return
        self.sales = DB.load("ventas")
        for i in self.reports_tree.get_children(): self.reports_tree.delete(i)
        total = 0.0
        for s in reversed(self.sales):
            self.reports_tree.insert("", "end", values=(s.get("fecha", ""), s.get("factura", ""), s.get("caja_id", "CAJA-01 (histórico)"), s.get("usuario", ""), s.get("metodo", ""), money(float(s.get("total", 0)))))
            total += float(s.get("total", 0))
        self.report_total.config(text=f"Ventas acumuladas: {money(total)}")
        if hasattr(self, "cashier_summary"):
            for iid in self.cashier_summary.get_children(): self.cashier_summary.delete(iid)
            totals = {}
            for sale in self.sales:
                name = sale.get("usuario", "Sin usuario")
                row = totals.setdefault(name, {"cantidad": 0, "total": 0.0})
                row["cantidad"] += 1
                row["total"] += float(sale.get("total", 0))
            for name, row in sorted(totals.items()):
                self.cashier_summary.insert("", "end", values=(name, row["cantidad"], money(row["total"])))
            if hasattr(self, "terminal_summary"):
                for iid in self.terminal_summary.get_children(): self.terminal_summary.delete(iid)
                terminal_totals = {}
                for sale in self.sales:
                    name = sale.get("caja_id", "CAJA-01 (histórico)")
                    row = terminal_totals.setdefault(name, {"cantidad": 0, "total": 0.0})
                    row["cantidad"] += 1
                    row["total"] += float(sale.get("total", 0))
                for name, row in sorted(terminal_totals.items()):
                    self.terminal_summary.insert("", "end", values=(name, row["cantidad"], money(row["total"])))

    def build_users_tab(self):
        cols = ("username", "role", "active", "created")
        self.users_tree = ttk.Treeview(self.users_tab, columns=cols, show="headings", height=12)
        for c, title, width in zip(cols, ("Usuario", "Rol", "Activo", "Creado"), (220, 120, 100, 220)):
            self.users_tree.heading(c, text=title); self.users_tree.column(c, width=width)
        self.users_tree.pack(fill="both", expand=True)
        form = ttk.LabelFrame(self.users_tab, text="Crear usuario", padding=8); form.pack(fill="x", pady=8)
        self.new_username = ttk.Entry(form, width=18); self.new_password = ttk.Entry(form, width=20, show="•")
        self.new_role = tk.StringVar(value="cajero")
        ttk.Label(form, text="Usuario").pack(side="left"); self.new_username.pack(side="left", padx=4)
        ttk.Label(form, text="Contraseña inicial").pack(side="left"); self.new_password.pack(side="left", padx=4)
        ttk.Label(form, text="Rol").pack(side="left"); ttk.Combobox(form, textvariable=self.new_role, values=ROLES, state="readonly", width=10).pack(side="left", padx=4)
        ttk.Button(form, text="Crear", command=self.create_user).pack(side="left", padx=5)
        actions = ttk.Frame(self.users_tab); actions.pack(fill="x")
        ttk.Button(actions, text="Activar / desactivar", command=self.toggle_user).pack(side="left", padx=3)
        ttk.Button(actions, text="Cambiar rol", command=self.change_user_role).pack(side="left", padx=3)
        ttk.Button(actions, text="Permisos individuales", command=self.edit_permissions).pack(side="left", padx=3)
        ttk.Button(actions, text="Restablecer contraseña", command=self.reset_user_password).pack(side="left", padx=3)
        ttk.Button(actions, text="Ver historial de actividades", command=self.show_audit).pack(side="left", padx=3)
        self.refresh_users()

    def refresh_users(self):
        if not hasattr(self, "users_tree"): return
        for i in self.users_tree.get_children(): self.users_tree.delete(i)
        for u in self.store.users:
            self.users_tree.insert("", "end", iid=u["username"], values=(u["username"], u.get("role"), "Sí" if u.get("active", True) else "No", u.get("created_at", "")))

    def selected_user(self):
        selection = self.users_tree.selection()
        if not selection:
            messagebox.showwarning("Usuarios", "Selecciona un usuario.")
            return None
        return self.store.get(selection[0])

    def create_user(self):
        try:
            self.store.create(self.new_username.get(), self.new_password.get(), self.new_role.get())
            audit(self.current_user["username"], "USUARIO_CREADO", self.new_username.get())
            self.new_username.delete(0, tk.END); self.new_password.delete(0, tk.END)
            self.refresh_users()
            messagebox.showinfo("Usuarios", "Usuario creado.")
        except Exception as e: messagebox.showerror("Usuarios", str(e))

    def edit_permissions(self):
        user = self.selected_user()
        if not user:
            return
        if user["username"] == self.current_user["username"]:
            return messagebox.showwarning("Permisos", "No puedes modificar tus propios permisos en esta sesión.")
        permissions = {
            "ventas": "Realizar ventas",
            "inventario_ver": "Consultar inventario",
            "inventario_gestionar": "Registrar y reponer productos",
            "reportes": "Consultar reportes",
            "usuarios": "Administrar usuarios",
            "caja_turnos": "Abrir/cerrar turnos y consultar caja",
            "caja_movimientos": "Registrar entradas y salidas de efectivo",
        }
        current = set(user.get("permissions", ROLE_DEFAULTS.get(user.get("role"), [])))
        win = tk.Toplevel(self); win.title("Permisos individuales"); win.transient(self); win.grab_set()
        ttk.Label(win, text=f"Permisos para {user['username']}", font=("Arial", 12, "bold")).pack(anchor="w", padx=12, pady=10)
        flags = {}
        for key, label in permissions.items():
            var = tk.BooleanVar(value=key in current); flags[key] = var
            ttk.Checkbutton(win, text=label, variable=var).pack(anchor="w", padx=14, pady=3)
        def save_permissions():
            selected = [key for key, var in flags.items() if var.get()]
            if user.get("role") == "admin" and user.get("active", True) and "usuarios" in current and "usuarios" not in selected:
                active_admins = [u for u in self.store.users if u.get("active", True) and u.get("role") == "admin" and "usuarios" in u.get("permissions", ROLE_DEFAULTS["admin"])]
                if len(active_admins) <= 1:
                    return messagebox.showerror("Permisos", "Debe quedar al menos un administrador activo con permiso de usuarios.", parent=win)
            user["permissions"] = selected
            self.store.save()
            audit(self.current_user["username"], "PERMISOS_MODIFICADOS", f"{user['username']}={','.join(selected)}")
            win.grab_release(); win.destroy()
            messagebox.showinfo("Permisos", "Permisos actualizados.")
        ttk.Button(win, text="Guardar", command=save_permissions).pack(pady=12)

    def toggle_user(self):
        u = self.selected_user()
        if not u: return
        if u["username"] == self.current_user["username"]:
            return messagebox.showwarning("Usuarios", "No puedes desactivar tu propia cuenta.")
        active = not u.get("active", True)
        if not active and u["role"] == "admin" and sum(1 for x in self.store.users if x["role"] == "admin" and x.get("active", True)) <= 1:
            return messagebox.showwarning("Usuarios", "Debe quedar al menos un administrador activo.")
        self.store.set_active(u, active); audit(self.current_user["username"], "ESTADO_USUARIO", f"{u['username']}={active}")
        self.refresh_users()

    def change_user_role(self):
        u = self.selected_user()
        if not u: return
        if u["username"] == self.current_user["username"]:
            return messagebox.showwarning("Usuarios", "No puedes cambiar tu propio rol.")
        role = simpledialog.askstring("Cambiar rol", "Escribe admin o cajero:", parent=self)
        if role:
            role = role.strip().lower()
            try:
                if u["role"] == "admin" and role != "admin" and sum(1 for x in self.store.users if x["role"] == "admin" and x.get("active", True)) <= 1:
                    raise ValueError("Debe quedar al menos un administrador activo.")
                self.store.set_role(u, role); audit(self.current_user["username"], "ROL_MODIFICADO", f"{u['username']}={role}")
                self.refresh_users()
            except Exception as e: messagebox.showerror("Usuarios", str(e))

    def reset_user_password(self):
        u = self.selected_user()
        if not u: return
        new = simpledialog.askstring("Restablecer contraseña", f"Nueva contraseña para {u['username']}:", show="•", parent=self)
        if new is None: return
        try:
            self.store.change_password(u, new)
            audit(self.current_user["username"], "CLAVE_RESTABLECIDA", u["username"])
            messagebox.showinfo("Usuarios", "Contraseña actualizada.")
        except Exception as e: messagebox.showerror("Usuarios", str(e))

    def show_audit(self):
        win = tk.Toplevel(self); win.title("Historial de actividades"); win.geometry("850x450")
        tree = ttk.Treeview(win, columns=("fecha", "usuario", "accion", "detalle"), show="headings")
        for c, title, width in (("fecha", "Fecha", 160), ("usuario", "Usuario", 130), ("accion", "Acción", 200), ("detalle", "Detalle", 320)):
            tree.heading(c, text=title); tree.column(c, width=width)
        tree.pack(fill="both", expand=True, padx=8, pady=8)
        for r in DB.audit_rows(1000):
            tree.insert("", "end", values=(r.get("fecha"), r.get("usuario"), r.get("accion"), r.get("detalle")))

    def change_own_password(self):
        old = simpledialog.askstring("Cambiar contraseña", "Contraseña actual:", show="•", parent=self)
        if old is None: return
        if not verify_password(old, self.current_user["salt"], self.current_user["password_hash"]):
            return messagebox.showerror("Seguridad", "La contraseña actual no es correcta.")
        new = simpledialog.askstring("Cambiar contraseña", "Nueva contraseña (mínimo 10 caracteres, letras y números):", show="•", parent=self)
        if new is None: return
        confirm = simpledialog.askstring("Cambiar contraseña", "Confirma la nueva contraseña:", show="•", parent=self)
        if new != confirm: return messagebox.showerror("Seguridad", "Las contraseñas no coinciden.")
        try:
            self.store.change_password(self.current_user, new)
            audit(self.current_user["username"], "CAMBIO_PROPIA_CLAVE")
            messagebox.showinfo("Seguridad", "Contraseña cambiada.")
        except Exception as e: messagebox.showerror("Seguridad", str(e))

    def logout(self):
        if self.current_open_shift():
            return messagebox.showwarning("Turno abierto", "Debes cerrar y arquear tu turno antes de cerrar sesión.")
        if self.current_user: audit(self.current_user["username"], "CIERRE_SESION")
        self.current_user = None
        self.cart.clear()
        for child in self.winfo_children(): child.destroy()
        self.login_dialog()

    def on_close(self):
        if self.current_user: audit(self.current_user["username"], "CIERRE_APLICACION")
        self.destroy()


if __name__ == "__main__":
    ensure_files()
    app = POSApp()
    app.mainloop()
