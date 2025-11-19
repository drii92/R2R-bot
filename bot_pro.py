import os
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- CONFIG (from env) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS","").split(",") if x]
SHEET_NAME = os.environ.get("SHEET_NAME", "R2R_Listings")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN missing in env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Google Sheets
def gsheet_client():
    if not GOOGLE_CREDS_JSON:
        raise Exception("GOOGLE_CREDS_JSON missing")
    if GOOGLE_CREDS_JSON.strip().startswith("{"):
        tmp = "/tmp/gcreds.json"
        with open(tmp, "w") as f:
            f.write(GOOGLE_CREDS_JSON)
        creds_path = tmp
    else:
        creds_path = GOOGLE_CREDS_JSON
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
    client = gspread.authorize(creds)
    return client

def ensure_sheet():
    client = gsheet_client()
    try:
        sh = client.open(SHEET_NAME)
    except Exception:
        sh = client.create(SHEET_NAME)
    ws = sh.sheet1
    header = [
        "timestamp","chat_id","user","city","price","m2",
        "rent_est","state","url","notes","photo_filename","contact"
    ]
    if not ws.row_values(1):
        ws.insert_row(header, 1)
    return ws

# Conversation states
(C_CITY, C_PRICE, C_M2, C_RENT, C_STATE, C_URL, C_PHOTO, C_CONTACT, C_CONFIRM) = range(9)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    kb = [
        [InlineKeyboardButton("Buscar por ciudad", callback_data="menu_search")],
        [InlineKeyboardButton("Enviar piso para análisis", callback_data="menu_submit")],
        [InlineKeyboardButton("Plantilla / Dosier", callback_data="menu_template")],
        [InlineKeyboardButton("Contactar admin", callback_data="menu_contact")],
    ]
    txt = f"Hola {user.first_name or ''}! Soy Ready2R Bot. Elige una opción."
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb))

async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "menu_search":
        await q.edit_message_text(
            "Escribe la ciudad que te interesa o usa /madrid /valencia etc."
        )
        return ConversationHandler.END

    if data == "menu_submit":
        await q.edit_message_text(
            "Empezamos. ¿En qué ciudad está el piso? (ej: Madrid)"
        )
        return C_CITY

    if data == "menu_template":
        await q.edit_message_text(
            "Plantilla: usa este formato:\n"
            "📍 Ubicación:\n💶 Precio:\n📐 m²:\n🔧 Estado:\n💸 Alquiler estimado:\n🔗 Enlace:"
        )
        return ConversationHandler.END

    if data == "menu_contact":
        await q.edit_message_text(
            "Contacta con el admin: escribe /contacto en el chat."
        )
        return ConversationHandler.END

    return ConversationHandler.END

# --- Conversación envío de piso ---

async def c_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["city"] = update.message.text.strip()
    await update.message.reply_text("Precio (ej. 139000)")
    return C_PRICE

async def c_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["price"] = update.message.text.strip()
    await update.message.reply_text("Metros (m²)")
    return C_M2

async def c_m2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["m2"] = update.message.text.strip()
    await update.message.reply_text("Alquiler estimado (€/mes)")
    return C_RENT

async def c_rent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["rent"] = update.message.text.strip()
    await update.message.reply_text("Estado del piso (Reformado / A reformar)")
    return C_STATE

async def c_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = update.message.text.strip()
    await update.message.reply_text("Enlace al anuncio (si lo tienes) o escribe 'no'")
    return C_URL

async def c_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["url"] = update.message.text.strip()
    await update.message.reply_text("Puedes enviar una foto ahora o escribir 'no'")
    return C_PHOTO

async def c_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        fname = f"photo_{update.effective_user.id}_{int(datetime.utcnow().timestamp())}.jpg"
        path = f"./uploads/{fname}"
        os.makedirs("./uploads", exist_ok=True)
        await file.download_to_drive(path)
        context.user_data["photo"] = path
    else:
        context.user_data["photo"] = ""
    await update.message.reply_text(
        "Contacto del propietario / tu contacto (teléfono o email) o 'no'"
    )
    return C_CONTACT

async def c_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["contact"] = update.message.text.strip()
    s = context.user_data
    summary = (
        "Resumen:\n"
        f"Ciudad: {s.get('city')}\n"
        f"Precio: {s.get('price')}\n"
        f"m2: {s.get('m2')}\n"
        f"Alquiler: {s.get('rent')}\n"
        f"Estado: {s.get('state')}\n"
        f"URL: {s.get('url')}\n"
        f"Contacto: {s.get('contact')}"
    )
    await update.message.reply_text(
        summary + "\n\nConfirma 'si' para guardar o 'no' para cancelar."
    )
    return C_CONFIRM

async def c_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if txt in ("si", "sí", "s"):
        ws = ensure_sheet()
        s = context.user_data
        row = [
            datetime.utcnow().isoformat(),
            update.effective_chat.id,
            update.effective_user.username or update.effective_user.full_name,
            s.get("city"),
            s.get("price"),
            s.get("m2"),
            s.get("rent"),
            s.get("state"),
            s.get("url"),
            "",
            s.get("photo", ""),
            s.get("contact"),
        ]
        ws.append_row(row)
        await update.message.reply_text(
            "Guardado. Gracias — un admin lo revisará y lo publicará si procede."
        )
        for a in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    a,
                    f"Nuevo piso enviado por {update.effective_user.full_name}: "
                    f"{s.get('city')} {s.get('price')}",
                )
            except Exception as e:
                logger.exception(e)
    else:
        await update.message.reply_text("Cancelado.")
    return ConversationHandler.END

async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("No autorizado")
    ws = ensure_sheet()
    rows = ws.get_all_values()[-10:]
    txt = "Últimos envíos:\n"
    for r in rows[-10:]:
        txt += f"- {r[0]} | {r[2]} | {r[3]} | {r[4]}€\n"
    await update.message.reply_text(txt)

async def city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt.startswith("/"):
        city = txt[1:]
        await update.message.reply_text(
            f"Buscando oportunidades en {city.capitalize()}... (pendiente de implementar consulta a Sheet)"
        )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception", exc_info=context.error)
    if ADMIN_IDS:
        for a in ADMIN_IDS:
            try:
                await context.bot.send_message(a, f"Error: {context.error}")
            except Exception:
                pass

def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(callback_menu, pattern=r"^menu_submit$")
        ],
        states={
            C_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_city)],
            C_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_price)],
            C_M2: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_m2)],
            C_RENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_rent)],
            C_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_state)],
            C_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_url)],
            C_PHOTO: [MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), c_photo)],
            C_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_contact)],
            C_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, c_confirm)],
        },
        fallbacks=[
            CommandHandler("cancel", lambda u, c: (u.message.reply_text("Cancelado."), ConversationHandler.END)[1])
        ],
    )

    app.add_handler(CommandHandler("start", start))
    # Conversación primero
    app.add_handler(conv)
    # Y luego el resto de botones (search / template / contact)
    app.add_handler(CallbackQueryHandler(callback_menu, pattern=r"^menu_(search|template|contact)$"))
    app.add_handler(CommandHandler("lista", admin_list))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, city_handler))
    app.add_error_handler(error_handler)
    return app

if __name__ == "__main__":
    app = build_app()
    app.run_polling(poll_interval=3)
