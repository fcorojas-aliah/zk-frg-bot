"""
ZK/FRG Gastos Bot — recibe fotos de estados de cuenta por Telegram,
los clasifica con Claude (ZK Operativo vs FRG Personal) y los guarda
en el Google Sheet en vivo.
"""

import os
import json
import base64
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("zkfrg-bot")

# ---------- Config (viene de variables de entorno en Railway) ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]  # el JSON completo, como texto
SHEET_ID = os.environ["SHEET_ID"]  # 1sJJhnz9tNqNkTuDG5g9QIaqybC39Ew6MF_7Z2MEb4Og

# IDs de Telegram autorizados a usar el bot (tu chat_id personal).
# Déjalo vacío ("") mientras pruebas; luego lo llenas para que nadie más lo use.
ALLOWED_USER_IDS = [x for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x]

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=90.0, max_retries=2)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(SHEET_ID)

CLASSIFY_PROMPT = """Eres el asistente financiero de Francisco Rojas García (FRG).
Te voy a mandar un estado de cuenta, que puede tener muchos movimientos.

Clasifica CADA cargo/movimiento que encuentres en uno de dos espacios:
- "ZK Operativo": gastos de ZK Inmobiliaria (Meta Ads, herramientas IA como Claude/Runway/Midjourney/VEED, HostGator, marketing, comisiones a colaboradores de ZK).
- "FRG Personal": todo lo demás — vivienda, transporte, pensión, salud, ropa, suscripciones, restaurantes, préstamos personales, etc.

Para cada cargo, asigna también una Sección:
- Si es FRG Personal: "Gastos Fijos", "Gastos Variables", "Tarjetas (MSI)", "Préstamos personales", u "Otro"
- Si es ZK Operativo: "Marketing", "Herramienta IA", "Tecnología", "Operación", u "Otro"

FORMATO DE RESPUESTA — MUY IMPORTANTE, léelo con cuidado:
Responde con UN OBJETO JSON POR LÍNEA (formato NDJSON) — uno por cada cargo del estado de cuenta.
NO uses un array. NO envuelvas todo en un objeto grande. NO agregues texto, explicaciones, ni marcadores de markdown (nada de ```).
Cada línea debe ser un JSON completo e independiente, con este formato exacto:

{"tarjeta": "nombre de la tarjeta o cuenta si se identifica, si no 'Sin identificar'", "fecha": "DD/MM/AAAA si aparece, si no ''", "concepto": "descripción del cargo", "monto": 0.00, "clasificacion": "ZK Operativo" o "FRG Personal", "seccion": "una de las opciones de arriba", "notas": ""}

Si el estado de cuenta tiene 40 movimientos, tu respuesta debe tener 40 líneas, una por cargo.
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
        messages=[{
            "role": "user",
            "content": [content_block, {"type": "text", "text": prompt}],
        }],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")

    cargos = []
    skipped = 0
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
            skipped += 1  # línea incompleta (ej. se cortó el último renglón) — se ignora, no tumba el resto

    return cargos, skipped


def append_to_sheet(cargos: list, source_note: str):
    ws_zk = sheet.worksheet("ZK Operativo")
    ws_frg = sheet.worksheet("FRG Personal")
    today = datetime.now().strftime("%d/%m/%Y")

    # Precarga montos ya existentes (columna D) para chequeo de duplicados
    def _existing_montos(ws):
        try:
            vals = ws.col_values(4)[1:]  # salta encabezado
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

    n_zk, n_frg, n_dup = 0, 0, 0
    for c in cargos:
        try:
            monto = round(float(c.get("monto", 0)), 2)
        except (TypeError, ValueError):
            monto = 0

        is_zk = c.get("clasificacion") == "ZK Operativo"
        existing_set = existing_zk if is_zk else existing_frg

        if monto in existing_set:
            n_dup += 1
            continue  # ya existe un cargo con este monto exacto — se omite

        row = [
            c.get("fecha") or today,
            c.get("tarjeta", "Sin identificar"),
            c.get("concepto", ""),
            monto,
            c.get("seccion", ""),
            "",  # subcategoría libre, ajustar a mano si hace falta
            c.get("notas", ""),
            source_note,
        ]
        if is_zk:
            ws_zk.append_row(row, value_input_option="USER_ENTERED")
            n_zk += 1
        else:
            ws_frg.append_row(row, value_input_option="USER_ENTERED")
            n_frg += 1
        existing_set.add(monto)  # evita duplicar dentro del mismo lote

    return n_zk, n_frg, n_dup


async def is_authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True  # modo prueba abierto — configura ALLOWED_USER_IDS antes de producción
    return str(update.effective_user.id) in ALLOWED_USER_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hola Francisco. Mándame la foto o PDF del estado de cuenta de una tarjeta "
        "y lo clasifico automático entre ZK Operativo y FRG Personal.\n\n"
        f"Tu chat_id es: {update.effective_user.id} (guárdalo para ALLOWED_USER_IDS)"
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

        cargos, skipped = classify_statement(bytes(image_bytes), "image/jpeg", user_note=update.message.caption or "")

        if not cargos:
            msg = "No identifiqué cargos en la imagen. ¿Está completa y legible?"
            if skipped:
                msg += f" ({skipped} línea(s) llegaron con formato raro y se descartaron)"
            await update.message.reply_text(msg)
            return

        n_zk, n_frg, n_dup = append_to_sheet(cargos, f"Telegram foto {datetime.now().strftime('%d/%m %H:%M')}")

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
    except Exception as e:
        log.exception("Error procesando estado de cuenta")
        await update.message.reply_text(f"Error al procesar: {e}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        await update.message.reply_text("No autorizado.")
        return

    doc = update.message.document
    if doc.mime_type not in ("image/jpeg", "image/png", "application/pdf"):
        await update.message.reply_text("Por ahora solo leo imágenes (JPG/PNG) o PDF. Manda una captura o el PDF del estado de cuenta.")
        return

    await update.message.reply_text("Recibido. Leyendo y clasificando...")
    try:
        file = await context.bot.get_file(doc.file_id)
        image_bytes = await file.download_as_bytearray()
        cargos, skipped = classify_statement(bytes(image_bytes), doc.mime_type, user_note=update.message.caption or "")

        if not cargos:
            msg = "No identifiqué cargos en el archivo."
            if skipped:
                msg += f" ({skipped} línea(s) llegaron con formato raro y se descartaron)"
            await update.message.reply_text(msg)
            return

        n_zk, n_frg, n_dup = append_to_sheet(cargos, f"Telegram doc {datetime.now().strftime('%d/%m %H:%M')}")
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
    except Exception as e:
        log.exception("Error procesando documento")
        await update.message.reply_text(f"Error al procesar: {e}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE | filters.Document.PDF, handle_document))
    log.info("Bot arrancando (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
