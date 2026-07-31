"""
ZK/FRG Gastos Bot — asistente financiero completo.

Capacidades:
1. Clasifica estados de cuenta (foto/PDF) entre ZK Operativo y FRG Personal.
2. Detecta y separa la tarjeta corporativa compartida con la socia (Evelyn) al 50/50,
   con precorte que Francisco debe confirmar antes de aplicarse.
3. Módulo de deudas: entiende instrucciones en texto libre ("mete esto al adeudo de
   Záruka, 35 mil...") y actualiza saldo + historial.
4. Responde preguntas libres sobre los datos ya guardados (sin necesidad de adjuntar nada).
5. Recordatorio automático cada 6 meses para revisar y ampliar el presupuesto a 18 meses.
"""

import os
import json
import base64
import logging
from datetime import datetime, time as dt_time

import gspread
from google.oauth2.service_account import Credentials
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("zkfrg-bot")

# ---------- Config ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SHEET_ID = os.environ["SHEET_ID"]
ALLOWED_USER_IDS = [x for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x]
REMINDER_START = datetime(2026, 8, 1)  # ancla para el ciclo de recordatorio semestral

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=90.0, max_retries=2)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(SHEET_ID)

# ---------- Estructura de hojas requeridas (se crean solas si faltan) ----------
REQUIRED_SHEETS = {
    "ZK Operativo": ["Fecha", "Cuenta/Tarjeta", "Concepto", "Monto MXN", "Sección", "Subcategoría", "Notas", "Fuente"],
    "FRG Personal": ["Fecha", "Cuenta/Tarjeta", "Concepto", "Monto MXN", "Sección", "Subcategoría", "Notas", "Fuente"],
    "Deudas Módulo": ["Persona/Concepto", "Monto Original", "Fecha Inicio", "Saldo Actual", "Pago Mensual Programado", "¿Cuadra con flujo?", "Última actualización", "Notas"],
    "Historial Pagos Deuda": ["Fecha", "Persona/Concepto", "Tipo", "Monto", "Saldo Resultante", "Fuente"],
    "Deudas a Favor": ["Persona", "Monto", "Fecha", "Notas"],
    "TDC Corporativa FRG": ["Fecha", "Concepto", "Monto Total", "Monto Francisco (50%)", "Monto Evelyn (50%)", "Status", "Fuente"],
}


def ensure_worksheets_exist():
    existing = [ws.title for ws in sheet.worksheets()]
    for name, headers in REQUIRED_SHEETS.items():
        if name not in existing:
            ws = sheet.add_worksheet(title=name, rows=300, cols=len(headers) + 2)
            ws.append_row(headers, value_input_option="USER_ENTERED")
            log.info(f"Hoja creada: {name}")

    if "Dashboard" not in existing:
        ws = sheet.add_worksheet(title="Dashboard", rows=40, cols=6)
        ws.update(values=[["DASHBOARD — PRESUPUESTO vs REAL (se actualiza solo)"]], range_name="A1")
        ws.update(values=[["Editable: solo la columna B (Presupuesto). Todo lo demás se calcula solo."]], range_name="A2")

        rows = [
            ["Categoría", "Presupuesto Mensual", "Real (Total Histórico)", "Diferencia"],
            ["GASTOS FIJOS (FRG)", 40025, "=SUMIF('FRG Personal'!E:E,\"Gastos Fijos\",'FRG Personal'!D:D)", "=C4-B4"],
            ["GASTOS VARIABLES (FRG)", 8230, "=SUMIF('FRG Personal'!E:E,\"Gastos Variables\",'FRG Personal'!D:D)", "=C5-B5"],
            ["TARJETAS MSI (FRG)", 22949, "=SUMIF('FRG Personal'!E:E,\"Tarjetas (MSI)\",'FRG Personal'!D:D)", "=C6-B6"],
            ["PRÉSTAMOS PERSONALES (FRG)", 14793, "=SUMIF('FRG Personal'!E:E,\"Préstamos personales\",'FRG Personal'!D:D)", "=C7-B7"],
            ["OTRO (FRG)", 0, "=SUMIF('FRG Personal'!E:E,\"Otro\",'FRG Personal'!D:D)", "=C8-B8"],
            ["TOTAL FRG PERSONAL", "=SUM(B4:B8)", "=SUM(C4:C8)", "=C9-B9"],
            ["", "", "", ""],
            ["MARKETING (ZK)", 5000, "=SUMIF('ZK Operativo'!E:E,\"Marketing\",'ZK Operativo'!D:D)", "=C11-B11"],
            ["HERRAMIENTA IA (ZK)", 725, "=SUMIF('ZK Operativo'!E:E,\"Herramienta IA\",'ZK Operativo'!D:D)", "=C12-B12"],
            ["TECNOLOGÍA (ZK)", 100, "=SUMIF('ZK Operativo'!E:E,\"Tecnología\",'ZK Operativo'!D:D)", "=C13-B13"],
            ["OPERACIÓN (ZK)", 0, "=SUMIF('ZK Operativo'!E:E,\"Operación\",'ZK Operativo'!D:D)", "=C14-B14"],
            ["OTRO (ZK)", 0, "=SUMIF('ZK Operativo'!E:E,\"Otro\",'ZK Operativo'!D:D)", "=C15-B15"],
            ["TOTAL ZK OPERATIVO", "=SUM(B11:B15)", "=SUM(C11:C15)", "=C16-B16"],
            ["", "", "", ""],
            ["DEUDA TOTAL ACTIVA (suma saldos)", "", "=SUMIF('Deudas Módulo'!D:D,\">0\")", ""],
            ["TDC CORPORATIVA — pendiente de confirmar (tu 50%)", "", "=SUMIF('TDC Corporativa FRG'!F:F,\"Pendiente\",'TDC Corporativa FRG'!D:D)", ""],
        ]
        ws.update(values=rows, range_name="A3")
        log.info("Hoja creada: Dashboard (con fórmulas live)")


# ================= PPTO MENSUAL (réplica del artefacto, 18 meses, ligada a la data real) =================

MONTHS_18 = ["Ago", "Sep", "Oct", "Nov", "Dic", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic", "Ene"]
YEARS_18 = [2026, 2026, 2026, 2026, 2026, 2027, 2027, 2027, 2027, 2027, 2027, 2027, 2027, 2027, 2027, 2027, 2027, 2028]


def ensure_ppto_mensual():
    existing = [ws.title for ws in sheet.worksheets()]

    if "Detalle TDC MSI" not in existing:
        ws = sheet.add_worksheet(title="Detalle TDC MSI", rows=100, cols=6)
        ws.append_row(["Tarjeta", "Concepto", "Monto Mensual", "Meses Restantes (desde hoy)", "Notas"], value_input_option="USER_ENTERED")
        log.info("Hoja creada: Detalle TDC MSI (vacía — se llena conforme detectes compras a meses reales)")

    if "PPTO Mensual" in existing:
        return

    month_labels = [f"{m} {y}" for m, y in zip(MONTHS_18, YEARS_18)]
    header = ["CONCEPTO"] + month_labels + ["TOTAL PERIODO"]

    rows_def = [
        ("INGRESOS", None),
        ("  Aliah (comisiones)", [0] * 18),
        ("  Rafa", [2000] * 18),
        ("  Otros ingresos", [12000] * 18),
        ("TOTAL INGRESOS", "sum"),
        ("", None),
        ("GASTOS FIJOS", None),
        ("  Renta", [9300] * 18),
        ("  Servicios (Luz/Agua/Gas/Tag)", [3500] * 18),
        ("  Gasolina Journey", [3200] * 18),
        ("  Internet/Súper/Telcel/Mascotas", [8180] * 18),
        ("  Pensión (líquida+colegiaturas+adic.)", [15845] * 18),
        ("TOTAL GASTOS FIJOS", "sum"),
        ("", None),
        ("GASTOS VARIABLES", None),
        ("  Gasolina Mini", [2700] * 18),
        ("  Uber Eats", [2000] * 18),
        ("  Suscripciones/streaming (recortables)", [3530] * 18),
        ("  Auto (seguro+mtto+tenencia+verif.)", [0] * 18),
        ("  Salud y medicamentos", [0] * 18),
        ("  Ropa y calzado", [0] * 18),
        ("  Otros variables", [0] * 18),
        ("TOTAL GASTOS VARIABLES", "sum"),
        ("", None),
        ("TARJETAS (MSI — se comprime solo desde 'Detalle TDC MSI')", None),
        ("  BBVA Dorada", "tdc"),
        ("  Invex Kekis", "tdc"),
        ("  Banamex Kekis", "tdc"),
        ("TOTAL TARJETAS", "sum"),
        ("", None),
        ("PRÉSTAMOS PERSONALES (desde 'Deudas Módulo')", "prestamos"),
        ("", None),
        ("AHORRO (Colchón — meta 9 meses de Fijos+Variables)", [3000] * 18),
        ("INVERSIONES (Largo plazo, líquido 72h)", [3000] * 18),
        ("", None),
        ("BALANCE MENSUAL", "balance"),
        ("BALANCE ACUMULADO", "acum"),
    ]

    all_rows = [header]
    row_num = 2  # fila 1 es el header
    section_start = {}
    balance_refs = {}

    def col_letter(c):
        return chr(64 + c) if c <= 26 else "A" + chr(64 + c - 26)

    for label, kind in rows_def:
        if kind is None:
            all_rows.append([label] + [""] * 19)
            if label.strip():
                section_start[label] = row_num
        elif isinstance(kind, list):
            all_rows.append([label] + kind + [f"=SUM(B{row_num}:S{row_num})"])
        elif kind == "sum":
            title_key = [k for k in section_start if section_start[k] < row_num]
            start_row = section_start[title_key[-1]] + 1
            end_row = row_num - 1
            cols = [f"=SUM({col_letter(c)}{start_row}:{col_letter(c)}{end_row})" for c in range(2, 20)]
            all_rows.append([label] + cols + [f"=SUM(B{row_num}:S{row_num})"])
            balance_refs[label] = row_num
        elif kind == "tdc":
            card = label.strip()
            cols = []
            for c in range(2, 20):
                offset = c - 2
                cols.append(
                    f'=SUMPRODUCT((\'Detalle TDC MSI\'!$A$2:$A$200="{card}")*({offset}<\'Detalle TDC MSI\'!$D$2:$D$200)*\'Detalle TDC MSI\'!$C$2:$C$200)'
                )
            all_rows.append([label] + cols + [f"=SUM(B{row_num}:S{row_num})"])
        elif kind == "prestamos":
            cols = ["=SUM('Deudas Módulo'!$E$2:$E$200)"] * 18
            all_rows.append([label] + cols + [f"=SUM(B{row_num}:S{row_num})"])
            balance_refs["PRÉSTAMOS"] = row_num
        elif kind == "balance":
            ing_r = balance_refs["TOTAL INGRESOS"]
            fij_r = balance_refs["TOTAL GASTOS FIJOS"]
            var_r = balance_refs["TOTAL GASTOS VARIABLES"]
            tdc_r = balance_refs["TOTAL TARJETAS"]
            pre_r = balance_refs["PRÉSTAMOS"]
            ahorro_r = row_num - 3
            inv_r = row_num - 2
            cols = [f"={col_letter(c)}{ing_r}-{col_letter(c)}{fij_r}-{col_letter(c)}{var_r}-{col_letter(c)}{tdc_r}-{col_letter(c)}{pre_r}-{col_letter(c)}{ahorro_r}-{col_letter(c)}{inv_r}" for c in range(2, 20)]
            all_rows.append([label] + cols + [f"=SUM(B{row_num}:S{row_num})"])
            balance_refs["balance"] = row_num
        elif kind == "acum":
            bal_r = balance_refs["balance"]
            cols = []
            for c in range(2, 20):
                if c == 2:
                    cols.append(f"=B{bal_r}")
                else:
                    cols.append(f"={col_letter(c-1)}{row_num}+{col_letter(c)}{bal_r}")
            all_rows.append([label] + cols + [""])
        row_num += 1

    ws = sheet.add_worksheet(title="PPTO Mensual", rows=len(all_rows) + 5, cols=20)
    ws.update(values=all_rows, range_name="A1", value_input_option="USER_ENTERED")
    ws.freeze(rows=1, cols=1)
    log.info("Hoja creada: PPTO Mensual (18 meses, formulada, ligada a Detalle TDC MSI y Deudas Módulo)")


# ================= CLASIFICACIÓN DE ESTADOS DE CUENTA =================

CLASSIFY_PROMPT = """Eres el asistente financiero de Francisco Rojas García (FRG).
Te voy a mandar un estado de cuenta, que puede tener muchos movimientos.

Clasifica CADA cargo/movimiento en uno de dos espacios:
- "ZK Operativo": gastos de ZK Inmobiliaria (Meta Ads, herramientas IA como Claude/Runway/Midjourney/VEED, HostGator, marketing, comisiones a colaboradores de ZK).
- "FRG Personal": todo lo demás — vivienda, transporte, pensión, salud, ropa, suscripciones, restaurantes, préstamos personales, etc.

Para cada cargo, asigna también una Sección:
- Si es FRG Personal: "Gastos Fijos", "Gastos Variables", "Tarjetas (MSI)", "Préstamos personales", u "Otro"
- Si es ZK Operativo: "Marketing", "Herramienta IA", "Tecnología", "Operación", u "Otro"

FORMATO DE RESPUESTA — MUY IMPORTANTE:
Responde con UN OBJETO JSON POR LÍNEA (formato NDJSON) — uno por cada cargo.
NO uses un array. NO envuelvas todo en un objeto grande. NO agregues texto, explicaciones, ni marcadores de markdown.
Cada línea debe ser un JSON completo e independiente, con este formato exacto:

{"tarjeta": "nombre de la tarjeta o cuenta si se identifica, si no 'Sin identificar'", "titular": "nombre del titular de la tarjeta si aparece en el documento, si no ''", "fecha": "DD/MM/AAAA si aparece, si no ''", "concepto": "descripción del cargo", "monto": 0.00, "clasificacion": "ZK Operativo" o "FRG Personal", "seccion": "una de las opciones de arriba", "notas": ""}

Si el estado de cuenta tiene 40 movimientos, tu respuesta debe tener 40 líneas.
"""


def classify_statement(file_bytes: bytes, media_type: str, user_note: str = ""):
    b64 = base64.b64encode(file_bytes).decode("utf-8")
    if media_type == "application/pdf":
        content_block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    else:
        content_block = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}

    prompt = CLASSIFY_PROMPT
    if user_note:
        prompt += f"\n\nNota de Francisco sobre este envío (tómala en cuenta): {user_note}"

    msg = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{"role": "user", "content": [content_block, {"type": "text", "text": prompt}]}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")

    cargos, skipped = [], 0
    for line in text.strip().splitlines():
        line = line.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "monto" in obj:
                cargos.append(obj)
            else:
                skipped += 1
        except json.JSONDecodeError:
            skipped += 1

    return cargos, skipped


def append_to_sheet(cargos: list, source_note: str):
    ws_zk = sheet.worksheet("ZK Operativo")
    ws_frg = sheet.worksheet("FRG Personal")
    today = datetime.now().strftime("%d/%m/%Y")

    def _existing_montos(ws):
        try:
            vals = ws.col_values(4)[1:]
            out = set()
            for v in vals:
                try:
                    out.add(round(float(str(v).replace(",", "")), 2))
                except ValueError:
                    continue
            return out
        except Exception:
            return set()

    existing_zk = _existing_montos(ws_zk)
    existing_frg = _existing_montos(ws_frg)

    zk_rows, frg_rows, n_dup = [], [], 0
    for c in cargos:
        try:
            monto = round(float(c.get("monto", 0)), 2)
        except (TypeError, ValueError):
            monto = 0

        is_zk = c.get("clasificacion") == "ZK Operativo"
        existing_set = existing_zk if is_zk else existing_frg

        if monto in existing_set:
            n_dup += 1
            continue

        row = [
            c.get("fecha") or today,
            c.get("tarjeta", "Sin identificar"),
            c.get("concepto", ""),
            monto,
            c.get("seccion", ""),
            "",
            c.get("notas", ""),
            source_note,
        ]
        (zk_rows if is_zk else frg_rows).append(row)
        existing_set.add(monto)

    if zk_rows:
        ws_zk.append_rows(zk_rows, value_input_option="USER_ENTERED")
    if frg_rows:
        ws_frg.append_rows(frg_rows, value_input_option="USER_ENTERED")

    return len(zk_rows), len(frg_rows), n_dup


# ================= TARJETA CORPORATIVA (SOCIA EVELYN) — 50/50 CON PRECORTE =================

def is_corporativa_statement(cargos: list, caption: str) -> bool:
    cap = (caption or "").lower()
    if any(k in cap for k in ("evelyn", "socia", "corporativa")):
        return True
    return any("evelyn" in (c.get("titular", "") or "").lower() for c in cargos)


def stage_corporativa(cargos: list, source_note: str):
    ws = sheet.worksheet("TDC Corporativa FRG")
    today = datetime.now().strftime("%d/%m/%Y")
    rows, total = [], 0.0
    for c in cargos:
        try:
            monto = round(float(c.get("monto", 0)), 2)
        except (TypeError, ValueError):
            monto = 0
        mitad = round(monto / 2, 2)
        rows.append([c.get("fecha") or today, c.get("concepto", ""), monto, mitad, mitad, "Pendiente", source_note])
        total += monto
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    return len(rows), total


def confirm_pending_corporativa():
    ws = sheet.worksheet("TDC Corporativa FRG")
    records = ws.get_all_values()
    if len(records) <= 1:
        return None
    data = records[1:]
    pending_idx = [i + 2 for i, r in enumerate(data) if len(r) > 5 and r[5] == "Pendiente"]
    if not pending_idx:
        return None
    total_mitad = 0.0
    for r in data:
        if len(r) > 5 and r[5] == "Pendiente":
            try:
                total_mitad += float(r[3])
            except (ValueError, IndexError):
                pass
    for row_num in pending_idx:
        ws.update_cell(row_num, 6, "Confirmado")
    return f"Confirmado — {len(pending_idx)} cargo(s) de la tarjeta corporativa.\nTu parte (50%): ${total_mitad:,.2f} MXN"


# ================= MÓDULO DE DEUDAS (texto libre) =================

DEBT_INTENT_PROMPT = """Eres el asistente financiero de Francisco Rojas García.
Analiza este mensaje y determina si es una instrucción sobre un movimiento de deuda.

Ejemplos que SÍ son movimiento de deuda:
- "Mete esto al adeudo de Záruka, 35 mil por préstamo en efectivo a mí" -> nuevo préstamo
- "Aboné 5000 a lo de Carmen" -> pago (reduce saldo)
- "Le presté 2000 a Rafael" -> deuda a favor (alguien le debe a Francisco)

Responde SOLO con JSON, sin texto adicional, sin markdown:
{"es_movimiento_deuda": true, "persona": "nombre normalizado", "tipo": "Nuevo préstamo" o "Pago" o "Ajuste" o "Deuda a favor", "monto": 0.00, "concepto": "breve"}

Si el mensaje NO es un movimiento de deuda (pregunta, saludo, comentario, confirmación tipo "ok"), responde exactamente:
{"es_movimiento_deuda": false}
"""


def classify_text_intent(text: str) -> dict:
    msg = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": f"{DEBT_INTENT_PROMPT}\n\nMensaje: {text}"}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"es_movimiento_deuda": False}


def apply_debt_movement(intent: dict) -> str:
    persona = intent.get("persona", "Sin identificar")
    tipo = intent.get("tipo", "Ajuste")
    try:
        monto = round(float(intent.get("monto", 0) or 0), 2)
    except (TypeError, ValueError):
        monto = 0
    concepto = intent.get("concepto", "")
    today = datetime.now().strftime("%d/%m/%Y")

    if tipo == "Deuda a favor":
        ws = sheet.worksheet("Deudas a Favor")
        ws.append_row([persona, monto, today, concepto], value_input_option="USER_ENTERED")
        return f"Anotado: {persona} te debe ${monto:,.2f} MXN — {concepto}.\n(Es solo dato de referencia, no dispara ningún cálculo automático.)"

    ws_mod = sheet.worksheet("Deudas Módulo")
    ws_hist = sheet.worksheet("Historial Pagos Deuda")

    records = ws_mod.get_all_values()[1:]
    row_idx, saldo_actual = None, 0.0
    for i, r in enumerate(records, start=2):
        if r and r[0].strip().lower() == persona.strip().lower():
            row_idx = i
            try:
                saldo_actual = float(str(r[3]).replace(",", "")) if len(r) > 3 and r[3] else 0
            except ValueError:
                saldo_actual = 0
            break

    if tipo == "Nuevo préstamo":
        nuevo_saldo = saldo_actual + monto
    else:
        nuevo_saldo = saldo_actual - monto

    if row_idx:
        ws_mod.update_cell(row_idx, 4, nuevo_saldo)
        ws_mod.update_cell(row_idx, 7, today)
    else:
        ws_mod.append_row(
            [persona, monto if tipo == "Nuevo préstamo" else 0, today, nuevo_saldo, 0, "Por definir", today, concepto],
            value_input_option="USER_ENTERED",
        )

    ws_hist.append_row([today, persona, tipo, monto, nuevo_saldo, "Telegram texto"], value_input_option="USER_ENTERED")

    return f"Listo — {tipo} de ${monto:,.2f} MXN aplicado a {persona}.\nSaldo actualizado: ${nuevo_saldo:,.2f} MXN"


# ================= PREGUNTAS LIBRES =================

ANSWER_PROMPT = """Eres el asistente financiero personal de Francisco Rojas García (FRG).
Te doy el contenido de varias hojas: gastos (ZK Operativo, FRG Personal), el módulo de deudas
(saldo por persona, si el flujo programado la resuelve o no) y deudas a favor (lo que le deben a Francisco).

Responde la pregunta de Francisco basándote ÚNICAMENTE en estos datos. Sé breve, directo, en español,
con números concretos cuando aplique. Si la información no está en los datos, dilo en vez de inventar.
"""


def answer_question(question: str) -> str:
    def _fmt(ws_name):
        ws = sheet.worksheet(ws_name)
        rows = ws.get_all_values()
        return f"=== {ws_name} ===\n" + "\n".join(" | ".join(r) for r in rows)

    context_text = "\n\n".join(
        _fmt(n) for n in ["ZK Operativo", "FRG Personal", "Deudas Módulo", "Deudas a Favor"]
    )

    msg = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": f"{ANSWER_PROMPT}\n\n{context_text}\n\nPregunta de Francisco: {question}"}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


# ================= HANDLERS =================

async def is_authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return str(update.effective_user.id) in ALLOWED_USER_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola Francisco. Puedo:\n"
        "- Leer fotos/PDF de estados de cuenta y clasificarlos (ZK vs FRG)\n"
        "- Detectar la tarjeta corporativa de Evelyn y separarla 50/50 (te pido confirmar)\n"
        "- Entender instrucciones de deuda en texto, ej: 'mete 35 mil al adeudo de Záruka'\n"
        "- Responder preguntas sobre tus datos ya guardados\n\n"
        f"Tu chat_id es: {update.effective_user.id} (guárdalo para ALLOWED_USER_IDS)"
    )


async def _process_and_reply(update, cargos, skipped, source_note, caption):
    if not cargos:
        msg = "No identifiqué cargos en el documento."
        if skipped:
            msg += f" ({skipped} línea(s) llegaron con formato raro y se descartaron)"
        await update.message.reply_text(msg)
        return

    if is_corporativa_statement(cargos, caption):
        n, total = stage_corporativa(cargos, source_note)
        await update.message.reply_text(
            f"Tarjeta corporativa (Evelyn) detectada — {n} cargo(s) por ${total:,.2f} MXN total.\n"
            f"Tu parte (50%): ${total/2:,.2f} MXN\n\n"
            f'Responde "ok" para confirmar y aplicarlo, o dime qué corregir.\n'
            f"Revisa el detalle: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
        )
        return

    n_zk, n_frg, n_dup = append_to_sheet(cargos, source_note)
    total = sum(c.get("monto", 0) for c in cargos)
    tarjetas = ", ".join(sorted(set(c.get("tarjeta", "Sin identificar") for c in cargos)))
    dup_line = f"\n{n_dup} omitido(s) por ser duplicado (mismo monto ya registrado)" if n_dup else ""
    skip_line = f"\n⚠️ {skipped} cargo(s) no se pudieron leer bien — si falta algo, manda menos páginas a la vez" if skipped else ""
    await update.message.reply_text(
        f"Listo — {tarjetas}\n"
        f"{len(cargos)} cargos detectados: {n_zk} ZK Operativo, {n_frg} FRG Personal{dup_line}{skip_line}\n"
        f"Total detectado: ${total:,.2f} MXN\n\n"
        f"Revisa el detalle: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        await update.message.reply_text("No autorizado.")
        return
    await update.message.reply_text("Recibido. Leyendo y clasificando...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        caption = update.message.caption or ""
        cargos, skipped = classify_statement(bytes(image_bytes), "image/jpeg", user_note=caption)
        await _process_and_reply(update, cargos, skipped, f"Telegram foto {datetime.now().strftime('%d/%m %H:%M')}", caption)
    except Exception as e:
        log.exception("Error procesando estado de cuenta")
        await update.message.reply_text(f"Error al procesar: {e}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        await update.message.reply_text("No autorizado.")
        return
    doc = update.message.document
    if doc.mime_type not in ("image/jpeg", "image/png", "application/pdf"):
        await update.message.reply_text("Por ahora solo leo imágenes (JPG/PNG) o PDF.")
        return
    await update.message.reply_text("Recibido. Leyendo y clasificando...")
    try:
        file = await context.bot.get_file(doc.file_id)
        image_bytes = await file.download_as_bytearray()
        caption = update.message.caption or ""
        cargos, skipped = classify_statement(bytes(image_bytes), doc.mime_type, user_note=caption)
        await _process_and_reply(update, cargos, skipped, f"Telegram doc {datetime.now().strftime('%d/%m %H:%M')}", caption)
    except Exception as e:
        log.exception("Error procesando documento")
        await update.message.reply_text(f"Error al procesar: {e}")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        await update.message.reply_text("No autorizado.")
        return

    text = update.message.text.strip()

    if text.lower() in ("ok", "si", "sí", "confirmar", "confirmo", "dale", "va", "correcto"):
        confirmed = confirm_pending_corporativa()
        if confirmed is not None:
            await update.message.reply_text(confirmed)
            return

    try:
        intent = classify_text_intent(text)
    except Exception:
        intent = {"es_movimiento_deuda": False}

    if intent.get("es_movimiento_deuda"):
        try:
            result = apply_debt_movement(intent)
            await update.message.reply_text(result)
        except Exception as e:
            log.exception("Error aplicando movimiento de deuda")
            await update.message.reply_text(f"Error al aplicar el movimiento: {e}")
        return

    await update.message.reply_text("Revisando tus datos...")
    try:
        answer = answer_question(text)
        await update.message.reply_text(answer)
    except Exception as e:
        log.exception("Error respondiendo pregunta")
        await update.message.reply_text(f"Error al responder: {e}")


# ================= RECORDATORIO SEMESTRAL =================

async def check_6month_reminder(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now()
    if today.day != 1:
        return
    months_diff = (today.year - REMINDER_START.year) * 12 + (today.month - REMINDER_START.month)
    if months_diff > 0 and months_diff % 6 == 0 and ALLOWED_USER_IDS:
        await context.bot.send_message(
            chat_id=ALLOWED_USER_IDS[0],
            text=(
                "📅 Recordatorio semestral — toca revisar tu presupuesto.\n\n"
                "1. Amplía la vista a 18 meses hacia adelante\n"
                "2. Revisa cada deuda: ¿el flujo programado sigue cuadrando o necesita ajuste?\n"
                "3. Actualiza montos fijos/variables si cambiaron\n\n"
                "Cuando quieras, dime y lo ajustamos juntos."
            ),
        )


# ================= MAIN =================

def main():
    ensure_worksheets_exist()
    ensure_ppto_mensual()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE | filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    if app.job_queue is not None:
        app.job_queue.run_daily(check_6month_reminder, time=dt_time(hour=9, minute=0))
    else:
        log.warning("JobQueue no disponible — instala python-telegram-bot[job-queue] para el recordatorio semestral")

    log.info("Bot arrancando (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
