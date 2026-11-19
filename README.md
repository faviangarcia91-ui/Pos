# POS Python 3.2.1 — Caja Registradora (optimizada)

Aplicación de escritorio Python + Tkinter con SQLite, control de usuarios, inventario, ventas y turnos de caja.

## 1. Requisitos

- Python 3.10 o superior (recomendado Python 3.11+).
- Tkinter disponible en la instalación de Python.
- SQLite incluido en la biblioteca estándar de Python.
- No necesita `pip install` ni paquetes externos.

En Windows, instala Python desde python.org y activa la opción **Add Python to PATH**. En algunas distribuciones Linux, Tkinter se instala aparte (por ejemplo, `python3-tk`).

## 2. Estructura

```text
POS_Python_3_2_Optimizado/
├── main.py                 # Interfaz gráfica Tkinter
├── database.py             # Capa SQLite y transacciones
├── migrar.py               # Importación segura desde POS 3.1 JSON
├── respaldar.py            # Copia consistente de SQLite
├── README.md
├── datos/
│   └── pos.db              # Se crea automáticamente
├── recibos/                # Comprobantes TXT
├── respaldos/              # Copias de seguridad de migración
└── tests/
    ├── test_pos31.py
    └── test_pos32.py
```

## 3. Migrar desde POS 3.1 (hacerlo antes de operar)

1. Cierra POS 3.1 en todas las terminales.
2. Haz una copia adicional de toda la carpeta POS 3.1.
3. Cierra todos los turnos abiertos en POS 3.1. La migración se detiene si encuentra más de un turno abierto, porque no puede asignarlos con seguridad a una sola caja.
4. Descomprime POS 3.2.1 en una carpeta nueva. No lo descomprimas encima de POS 3.1.
5. Abre una terminal dentro de la carpeta `POS_Python_3_2`.
6. Ejecuta el comando según tu sistema:

**Windows (PowerShell):**

```powershell
py migrar.py --origen "..\POS_Python_3_1\datos" --terminal CAJA-01
```

**Windows (CMD):**

```bat
py migrar.py --origen "..\POS_Python_3_1\datos" --terminal CAJA-01
```

**Linux / macOS:**

```bash
python3 migrar.py --origen ../POS_Python_3_1/datos --terminal CAJA-01
```

Ajusta la ruta `--origen` a la ubicación real de la carpeta `datos` de POS 3.1.

La migración:

- Crea una copia de los JSON y del registro de auditoría dentro de `respaldos/backup_json_FECHA_HORA/`.
- Importa productos, ventas, usuarios, permisos, turnos, movimientos de efectivo y auditoría.
- Conserva los hashes de contraseña; no necesita conocer ni restablecer las claves.
- Asigna a los registros históricos el identificador de caja indicado en `--terminal`.
- Compara los conteos de registros importados y ejecuta `PRAGMA integrity_check`.
- Genera `manifest.json` con conteos y resultado de la validación.
- No modifica ni borra los JSON originales.

Si la base de datos ya contiene registros, el programa de migración se detiene para evitar mezclar o duplicar información. Si la validación falla, no uses esa base para operar: conserva el respaldo, revisa el error y repite la migración en una carpeta limpia.

**Importante:** POS 3.2.1 no importa automáticamente los recibos TXT antiguos; consérvalos junto con el respaldo de POS 3.1. Los datos históricos de ventas y turnos sí se importan.

## 4. Ejecutar POS 3.2.1

En la misma carpeta:

```bash
python main.py
```

En Windows también puedes usar:

```bat
py main.py
```

La primera ejecución crea la base de datos y solicita crear el primer usuario administrador si todavía no hay usuarios.

## 5. Configurar varias cajas

Cada instancia debe tener un identificador distinto. La base SQLite debe estar en una ubicación local compartida por los procesos que se ejecutan en el mismo equipo.

**Windows PowerShell:**

```powershell
$env:POS_TERMINAL_ID="CAJA-02"
py main.py
```

**Windows CMD:**

```bat
set POS_TERMINAL_ID=CAJA-02 && py main.py
```

**Linux / macOS:**

```bash
POS_TERMINAL_ID=CAJA-02 python3 main.py
```

Si no defines la variable, se usa `CAJA-01`. Usa un identificador estable por terminal, por ejemplo `CAJA-01`, `CAJA-02`, `CAJA-03`. Cada turno y cada venta queda asociado a su caja.

### Alcance real de varias cajas

Esta versión soporta varias instancias/procesos que acceden al mismo archivo SQLite en un mismo equipo o almacenamiento local compatible con el bloqueo de SQLite. **No se recomienda colocar `pos.db` en una carpeta de red SMB/NAS ni usarlo simultáneamente desde varios computadores por Wi-Fi.** Para terminales en computadores distintos se debe migrar la capa de datos a un servidor de base de datos (por ejemplo, PostgreSQL) y desplegar un servicio central; eso no está incluido en esta versión.

## 6. Cómo evita conflictos

- `BEGIN IMMEDIATE` serializa las escrituras críticas de SQLite.
- La venta valida nuevamente el stock directamente en la base de datos, no confía en el stock que la pantalla pudo haber cargado antes.
- Descuento de existencias y registro de factura se confirman en una sola transacción. Si falla un producto, no se guarda ninguna parte de la venta.
- La factura tiene una clave única y cada intento de cobro usa una clave de solicitud para rechazar un reintento duplicado dentro de la misma sesión.
- Apertura y cierre de turnos validan el estado dentro de una transacción.
- Las salidas de efectivo se vuelven a validar con el saldo calculado desde la base de datos.
- SQLite usa WAL, `busy_timeout` y `synchronous=FULL`.

## 7. Funciones incluidas

- Inicio de sesión y usuarios `admin` / `cajero`.
- Contraseñas con PBKDF2-HMAC-SHA256, sal aleatoria individual y 600.000 iteraciones.
- Permisos individuales, activación/desactivación y cambio/restablecimiento de clave.
- Inventario, creación de productos y reposición de existencias.
- Carrito, descuento, IVA configurable, efectivo/tarjeta/transferencia y cambio.
- Recibos TXT.
- Turnos por caja, efectivo inicial, entradas/salidas, cierre y arqueo.
- Reportes por cajero y por caja.
- Auditoría de acciones en SQLite.
- Validación de importes no finitos y no negativos.

## 8. Pruebas automatizadas

Desde la carpeta del proyecto:

```bash
python -m unittest discover -s tests -v
```

Las pruebas cubren hashes, cálculo de efectivo, validación numérica, migración y preservación del origen, integridad de la base, apertura de turnos, bloqueo de reintentos y dos ventas concurrentes que compiten por la última unidad.

## 9. Respaldo y restauración

La versión 3.2.1 incluye una copia consistente usando la API de respaldo de SQLite (también funciona con WAL). Ejecuta:

```bash
python respaldar.py
```

El archivo verificado se guarda en `respaldos/sqlite/`. Para guardar en otra carpeta:

```bash
python respaldar.py --destino "D:/CopiasPOS"
```

Para restaurar: cierra todas las instancias del POS, conserva una copia de la base actual, reemplaza `datos/pos.db` por el archivo de respaldo y vuelve a iniciar la aplicación. No reemplaces la base mientras el POS esté abierto. Conserva también `recibos/` y los respaldos JSON de la migración.

No elimines los respaldos JSON originales hasta comprobar ventas, usuarios, productos y turnos en POS 3.2.1.

## 10. Mejoras de optimización en POS 3.2.1

- Corregido el guardado de productos existentes: la actualización SQLite ahora usa la clave primaria correcta.
- Las ventas consolidan cantidades repetidas del mismo código antes de validar stock, evitando que líneas duplicadas descuenten existencias incorrectamente.
- La validación de venta, stock, turno, factura y clave idempotente se ejecuta dentro de una sola transacción.
- Se rechazan importes no finitos, cero o negativos en movimientos de efectivo, y conteos de cierre inválidos.
- La migración ahora importa las colecciones en una sola transacción; si encuentra JSON inválido, claves duplicadas o conteos distintos, revierte la importación completa.
- Se añadieron índices SQLite para consultar ventas y movimientos por turno, cajero y caja; el arqueo ya no necesita recorrer todos los movimientos históricos.
- Se añadió `respaldar.py`, que crea copias consistentes con la API SQLite y verifica la integridad del archivo resultante.
- Se actualizaron los títulos visibles de la aplicación a POS 3.2.1.

## 11. Límites y verificaciones pendientes

- Se validaron sintaxis y pruebas automatizadas; la ventana Tkinter debe probarse manualmente en un escritorio real.
- La base de datos local no es una solución cliente-servidor para varios computadores.
- No incluye facturación electrónica, documento equivalente electrónico, integración DIAN, impresora fiscal ni certificación tributaria. **No se afirma cumplimiento DIAN.**
- El IVA inicial del 19% es solo un valor predeterminado de ejemplo; configura y valida el tratamiento tributario aplicable con tu contador.
- No incluye sincronización en la nube, pagos bancarios, devoluciones, cierres Z fiscales ni conciliación bancaria.
- Protege la carpeta de datos y realiza copias de seguridad frecuentes. Quien tenga acceso al sistema operativo y al archivo de base de datos puede modificar los datos fuera de la aplicación.

Antes de usarlo con dinero real, prueba apertura, ventas por cada método de pago, entradas/salidas, cierre, diferencia de caja, reposición y recuperación desde respaldo.


## 12. Pruebas de POS 3.2.1

Ejecuta `python -m unittest discover -s tests -v`. La suite actual contiene 16 pruebas que cubren hashes, números inválidos, actualización de productos existentes, consolidación de artículos repetidos, ventas concurrentes, idempotencia, turnos por terminal, arqueo, movimientos de caja, migración atómica, conservación del origen y copias SQLite verificadas.
