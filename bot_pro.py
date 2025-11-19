# bot_pro.py
# Ready2Rent - Bot PRO (versión lista para pegar)
# Reemplaza TODO el contenido anterior por este archivo.
# Requisitos: python-telegram-bot==20.3, gspread, oauth2client

import os
import logging
from datetime import datetime
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)

# Google Sheets
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ----------------------------
# CONFIG desde VARIABLES DE ENTORNO
# ----------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x]
SHEET_NAME = os.environ.get("SHEET_NAME", "R2R_Listings")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")  # recomendado
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")

# Validación mínima
if not BOT_TOKEN:
    raise Exception("BOT_TOKEN missing in env")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("r2r-bot")

# ----------------------------
# Google Sheets helpers
# ----------------------------
def gsheet_client():
    """
    Inicializa cliente gspread usando la variable GOOGLE_CREDS_JSON
    Soporta tanto JSON pegado en la var de entorno como ruta a fichero.
    """
    if not GOOGLE_CREDS_JSON:
        raise Exception("GOOGLE_CREDS_JSON missing in env")

    # Si la variable contiene JSON crudo, lo volcamos a /tmp/gcreds.json
    if GOOGLE_CREDS_JSON.strip().startswith("{"):
        tmp_path = "/tmp/gcreds.json"
        with open(tmp_path, "w") as f:
            f.write(GOOGLE_CREDS_JSON)
        creds_path = tmp_path
    else:
        creds_path = GOOGLE_CREDS_JSON  # asumimos ruta

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
    client = gspread.authorize(creds)
    return client

def ensure_sheet():
    """
    Devuelve el worksheet principal. Intenta abrir por SPREADSHEET_ID si existe,
    si no intenta abrir por nombre SHEET_NAME. No crea hojas en Drive de la
    cuenta service account (para evitar quota issues). Si no existe, lanza excepción.
    """
    client = gsheet_client()
    try:
        if SPREADSHEET_ID:
            sh = client.open_by_key(SPREADSHEET_ID)
        else:
            sh = client.open(SHEET_NAME)
    except Exception as e:
        # Mensaje claro para que el admin solucione: crear la hoja y compartirla
        logger.exception("Error abriendo la hoja en Google Sheets")
        raise Exception(
            "No se pudo abrir la hoja en Google Sheets. "
            "Asegúrate de crear la hoja y compartirla con el client_email de la service account, "
            "o define SPREADSHEET_ID en env."
        ) from e

    ws = sh.sheet1
    header = [
        "timestamp",
        "chat_id",
        "user",
        "city",
        "price",
        "m2",
        "rent_est",
        "state",
        "url",
        "notes",
        "photo_filename",
        "contact",
    ]
    try:
        first_row = ws.row_values(1)
    except Exception:
        first_row = []
    if not first_row:
        try:
            ws.insert_row(header, 1)
        except Exception:
            # si falla insertar cabecera (permiso, quota), lo ignoramos pero avisamos
            logger.warning("No se pudo insertar header en la hoja (posible falta de permisos).")
    return ws

# ----------------------------
# Conversation states
# ----------------------------
(
    C_CITY,
    C_PRICE,
    C_M2,
    C_RENT,
    C_STATE,
    C_URL,
    C_PHOTO,
    C_CONTACT,
    C_CONFIRM,
) = range(9)

# Cache username del bot
BOT_USERNAME = None

async def get_bot_username(context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME
    if BOT_USERNAME:
        return BOT_USERNAME
    me = await context.bot.get_me()
    BOT_USERNAME = me.username
    return BOT_USERNAME

# ----------------------------
# /start - SOLO en privado muestra el menú
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    bot_username = await get_bot_username(context)

    if chat.type == "private":
        kb = [
            [InlineKeyboardButton("Buscar por ciudad", callback_data="menu_search")],
            [InlineKeyboardButton("Enviar piso para análisis", callback_data="menu_submit")],
            [InlineKeyboardButton("Plantilla / Dosier", callback_data="menu_template")],
            [InlineKeyboardButton("Contactar admin", callback_data="menu_contact")],
        ]
        txt = f"Hola {user.first_name or ''}! Soy Ready2R Bot. Elige una opción."
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    else:
        # Si se usa en grupo, damos instrucción breve y no iniciamos formulario
        msg = (
            "Para enviar un piso o usar el formulario, háblame por privado 👉 "
            f"@{bot_username} y escribe /start\n\n"
            "En este grupo usa comandos públicos como /madrid o /valencia."
        )
        # Respond as reply to reduce noise
        if update.message:
            await update.message.reply_text(msg)
        else:
            await context.bot.send_message(chat.id, msg)

# ----------------------------
# CallbackQuery handler: si el keyboard está en grupo -> fuerza DM
# Si está en privado procesa normalmente.
# ----------------------------
async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    chat = q.message.chat
    user = q.from_user
    bot_username = await get_bot_username(context)

    # Si el teclado fue mostrado en un grupo -> pedimos DM
    if chat.type in ("group", "supergroup", "channel"):
        warn = (
            "Este formulario solo funciona en privado.\n\n"
            f"Pulsa aquí 👉 @{bot_username} y escribe /start para abrir el menú privado."
        )
        # Intentamos editar el mensaje para reducir ruido; si no se puede, enviamos un DM
        try:
            await q.edit_message_text("Este formulario solo funciona en privado. Comprueba tu chat con el bot.")
        except Exception:
            pass
        # Intentar enviar aviso privado al usuario
        try:
            await context.bot.send_message(user.id, warn)
        except Exception:
            # Si no puede DM, enviar un mensaje corto en el grupo (menos intrusivo)
            try:
                await context.bot.send_message(chat.id, f"{user.first_name}, revisa tu chat privado con @{bot_username}.")
            except Exception:
                pass
        return ConversationHandler.END

    # Si estamos en privado, procesamos cada opción
    if data == "menu_search":
        await q.edit_message_text("Escribe la ciudad que te interesa o usa /madrid /valencia etc.")
        return ConversationHandler.END

    if data == "menu_submit":
        await q.edit_message_text("Empezamos. ¿En qué ciudad está el piso? (ej: Madrid)")
        return C_CITY

    if data == "menu_template":
        await q.edit_message_text(
            "Plantilla: usa este formato:\n"
            "📍 Ubicación:\n💶 Precio:\n📐 m²:\n🔧 Estado:\n💸 Alquiler estimado:\n🔗 Enlace:"
        )
        return ConversationHandler.END

    if data == "menu_contact":
        await q.edit_message_text("Contacta con el admin: escribe /contacto en el chat.")
        return ConversationHandler.END

    return ConversationHandler.END

# ----------------------------
# Conversational flow: enviar piso - funciones por estado
# ----------------------------
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
    # Soportamos foto o 'no'
    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        fname = f"photo_{update.effective_user.id}_{int(datetime.utcnow().timestamp())}.jpg"
        path = f"./uploads/{fname}"
        os.makedirs("./uploads", exist_ok=True)
        try:
            await file.download_to_drive(path)
            context.user_data["photo"] = path
        except Exception:
            # si falla guardar, lo registramos y seguimos
            logger.exception("Error descargando la foto")
            context.user_data["photo"] = ""
    else:
        context.user_data["photo"] = ""
    await update.message.reply_text("Contacto del propietario / tu contacto (teléfono o email) o 'no'")
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
    await update.message.reply_text(summary + "\n\nConfirma 'si' para guardar o 'no' para cancelar.")
    return C_CONFIRM

async def c_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if txt in ("si", "sí", "s"):
        try:
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
            await update.message.reply_text("Guardado. Gracias — un admin lo revisará y lo publicará si procede.")
            # Notificar admins
            for a in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        a,
                        f"Nuevo piso enviado por {update.effective_user.full_name}: {s.get('city')} {s.get('price')}",
                    )
                except Exception:
                    logger.exception("Error notificando admin")
        except Exception as e:
            logger.exception("Error guardando en sheet")
            await update.message.reply_text(
                "Hubo un problema guardando el piso. Avisaré a un admin para que lo revise."
            )
            # Notify admin with the error
            for a in ADMIN_IDS:
                try:
                    await context.bot.send_message(a, f"Error guardando envío: {e}")
                except Exception:
                    pass
    else:
        await update.message.reply_text("Cancelado.")
    return ConversationHandler.END

# ----------------------------
# Admin command: lista (últimos envíos)
# ----------------------------
async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("No autorizado")
    try:
        ws = ensure_sheet()
        rows = ws.get_all_values()[-10:]
        txt = "Últimos envíos:\n"
        for r in rows[-10:]:
            txt += f"- {r[0]} | {r[2]} | {r[3]} | {r[4]}€\n"
        await update.message.reply_text(txt)
    except Exception as e:
        logger.exception("Error en admin_list")
        await update.message.reply_text("Error leyendo la hoja. Revisa permisos y SPREADSHEET_ID.")

# ----------------------------
# /madrid or /city simple handler (public)
# ----------------------------
async def city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if not txt.startswith("/"):
        return
    city = txt[1:].strip()
    # Aquí solo una respuesta placeholder. Más adelante se puede implementar consulta a Sheets
    await update.message.reply_text(f"Buscando oportunidades en {city.capitalize()}... (próximamente).")

# ----------------------------
# Welcome handler: nuevos miembros en grupo
# ----------------------------
async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    for member in update.message.new_chat_members:
        name = member.full_name or member.first_name or "nuevo miembro"
        bot_username = await get_bot_username(context)
        text = (
            f"Bienvenido/a, {name} 👋\n\n"
            "Este es Ready2R — comunidad de inversores de pisos listos para alquilar.\n\n"
            f"Para enviar un piso para análisis, háblame en privado 👉 @{bot_username} y escribe /start\n"
            "Para buscar por ciudad usa /madrid, /valencia, etc.\n\n"
            "Lee las reglas en el mensaje fijado y preséntate en el hilo de Presentaciones."
        )
        try:
            await context.bot.send_message(chat.id, text)
        except Exception:
            logger.exception("Error enviando bienvenida en el grupo")

        # Intento de DM al usuario (si permite DMs)
        try:
            dm = (
                f"Hola {member.first_name or ''}! Bienvenido a Ready2R.\n\n"
                "Si quieres enviar un piso para análisis, háblame aquí 👉 "
                f"@{bot_username} y escribe /start"
            )
            await context.bot.send_message(member.id, dm)
        except Exception:
            # el usuario puede tener DMs cerrados; lo ignoramos
            pass

# ----------------------------
# Error handler
# ----------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception", exc_info=context.error)
    # Notificar a admins
    for a in ADMIN_IDS:
        try:
            await context.bot.send_message(a, f"Error en bot: {context.error}")
        except Exception:
            pass

# ----------------------------
# Build app and register handlers
# ----------------------------
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Conversation handler (entrada por callback menu 'menu_submit' en privado)
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_menu, pattern=r"^menu_submit$")],
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
            CommandHandler(
                "cancel",
                lambda u, c: (u.message.reply_text("Cancelado."), ConversationHandler.END)[1],
            )
        ],
        allow_reentry=True,
    )

    # Handlers registration order matters
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    # CallbackQueryHandler para las opciones públicas (search/template/contact) en privado,
    # y que en grupo forcen DM (callback_menu maneja ambos casos).
    app.add_handler(CallbackQueryHandler(callback_menu, pattern=r"^menu_(search|template|contact)$"))
    app.add_handler(CommandHandler("lista", admin_list))
    # city commands (e.g. /madrid)
    app.add_handler(MessageHandler(filters.Regex(r"^/[a-zA-ZñÑáéíóúÁÉÍÓÚ]+$"), city_handler))
    # Welcome new members
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))
    # Error handler
    app.add_error_handler(error_handler)

    return app

# ----------------------------
# Entrypoint
# ----------------------------
if __name__ == "__main__":
    logger.info("Starting Ready2R Bot...")
    app = build_app()
    # run polling (ya estás sobre Render en background worker)
    app.run_polling(poll_interval=3)
