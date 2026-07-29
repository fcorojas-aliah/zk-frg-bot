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
Te voy a mandar la foto de un estado de cuenta de tarjeta de crédito.

Clasifica CADA cargo/movimiento que encuentres en uno de dos espacios:
- "ZK Operativo": gastos de ZK Inmobiliaria (Meta Ads, herramientas IA como Claude/Runway/Midjourney/VEED, HostGator, marketing, comisiones a colaboradores de ZK).
- "FRG Personal": todo lo demás — vivienda, transporte, pensión, salud, ropa, suscripciones, restaurantes, préstamos personales, etc.

Para cada cargo, asigna también una Sección:
- Si es FRG Personal: "Gastos Fijos", "Gastos Variables", "Tarjetas (MSI)", "Préstamos personales", u "Otro"
- Si es ZK Operativo: "Marketing", "Herramienta IA", "Tecnología", "Operación", u "Otro"

Responde SOLO con JSON válido, sin texto adicional, sin markdown, en este formato exacto:

{
  "tarjeta": "nombre de la tarjeta si se identifica en el estado de cuenta, si no 'Sin identificar'",
  "cargos": [
    {
      "fecha": "DD/MM/AAAA si aparece, si no ''",
      "concepto": "descripción del cargo",
      "monto": 0.00,
      "clasificacion": "ZK Operativo" o "FRG Personal",
      "seccion": "una de las opciones de arriba",
      "notas": ""
    }
  ]
}
"""

def classify_statement(image_bytes: bytes, media_type: str) -> dict:
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    msg = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": CLASSIFY_PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def append_to_sheet(tarjeta: str, cargos: list, source_note: str):
    ws_zk = sheet.worksheet("ZK Operativo")
    ws_frg = sheet.worksheet("FRG Personal")
    today = datetime.now().strftime("%d/%m/%Y")

    n_zk, n_frg = 0, 0
    for c in cargos:
        row = [
            c.get("fecha") or today,
            tarjeta,
            c.get("concepto", ""),
            c.get("monto", 0),
            c.get("seccion", ""),
            "",  # subcategoría libre, ajustar a mano si hace falta
            c.get("notas", ""),
            source_note,
        ]
        if c.get("clasificacion") == "ZK Operativo":
            ws_zk.append_row(row, value_input_option="USER_ENTERED")
            n_zk += 1
        else:
            ws_frg.append_row(row, value_input_option="USER_ENTERED")
            n_frg += 1
    return n_zk, n_frg


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

        result = classify_statement(bytes(image_bytes), "image/jpeg")
        tarjeta = result.get("tarjeta", "Sin identificar")
        cargos = result.get("cargos", [])

        if not cargos:
            await update.message.reply_text("No identifiqué cargos en la imagen. ¿Está completa y legible?")
            return

        n_zk, n_frg = append_to_sheet(tarjeta, cargos, f"Telegram foto {datetime.now().strftime('%d/%m %H:%M')}")

        total = sum(c.get("monto", 0) for c in cargos)
        await update.message.reply_text(
            f"Listo — {tarjeta}\n"
            f"{len(cargos)} cargos clasificados: {n_zk} ZK Operativo, {n_frg} FRG Personal\n"
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
    if doc.mime_type not in ("image/jpeg", "image/png"):
        await update.message.reply_text("Por ahora solo leo imágenes (JPG/PNG). Manda una captura del estado de cuenta.")
        return

    await update.message.reply_text("Recibido. Leyendo y clasificando...")
    try:
        file = await context.bot.get_file(doc.file_id)
        image_bytes = await file.download_as_bytearray()
        result = classify_statement(bytes(image_bytes), doc.mime_type)
        tarjeta = result.get("tarjeta", "Sin identificar")
        cargos = result.get("cargos", [])

        if not cargos:
            await update.message.reply_text("No identifiqué cargos en el archivo.")
            return

        n_zk, n_frg = append_to_sheet(tarjeta, cargos, f"Telegram doc {datetime.now().strftime('%d/%m %H:%M')}")
        total = sum(c.get("monto", 0) for c in cargos)
        await update.message.reply_text(
            f"Listo — {tarjeta}\n"
            f"{len(cargos)} cargos clasificados: {n_zk} ZK Operativo, {n_frg} FRG Personal\n"
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
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    log.info("Bot arrancando (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
