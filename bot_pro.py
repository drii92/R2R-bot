# bot_pro.py
# Ready2Rent - Bot PRO (con menú: Busco / Vendo / Manuales / Contacto)
# Requisitos:
# pip install python-telegram-bot==20.3 gspread oauth2client

import os
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

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

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ----------------------------
# CONFIG desde VARIABLES DE ENTORNO
# ----------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x]
ADMIN_NOTIFY = os.environ.get("ADMIN_NOTIFY", "@juanpedro233")  # puede ser @username o id
SHEET_NAME = os.environ.get("SHEET_NAME", "R2R_Listings")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")  # recomendado
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")
SAMPLE_PDF_URL = os.environ.get("SAMPLE_PDF_URL", "https://example.com/calculadora_rentabilidad.pdf")

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN missing in env")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("r2r-bot")

# ----------------------------
# Google Sheets helpers
# ----------------------------
def gsheet_client():
    if not GOOGLE_CREDS_JSON:
        raise Exception("GOOGLE_CREDS_JSON missing in env")
    if GOOGLE_CREDS_JSON.strip().startswith("{"):
        tmp_path = "/tmp/gcreds.json"
        with open(tmp_path, "w") as f:
            f.write(GOOGLE_CREDS_JSON)
        creds_path = tmp_path
    else:
        creds_path = GOOGLE_CREDS_JSON
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
    client = gspread.authorize(creds)
    return client

def ensure_sheet():
    client = gsheet_client()
    try:
        if SPREADSHEET_ID:
            sh = client.open_by_key(SPREADSHEET_ID)
        else:
            sh = client.open(SHEET_NAME)
    except Exception as e:
        logger.exception("Error abriendo la hoja en Google Sheets")
        raise Exception(
            "No se pudo abrir la hoja en Google Sheets. "
            "Crea la hoja y compártela con el client_email de la service account, "
            "o define SPREADSHEET_ID."
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
            logger.warning("No se pudo insertar header (posible falta de permisos).")
    return ws

# ----------------------------
# Conversation states (venta y contacto)
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
    CONTACT_MSG,
) = range(10)

# cache bot username
BOT_USERNAME = None

async def get_bot_username(context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME
    if BOT_USERNAME:
        return BOT_USERNAME
    me = await context.bot.get_me()
    BOT_USERNAME = me.username
    return BOT_USERNAME

# ----------------------------
# Menú principal (solo en privado)
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    bot_username = await get_bot_username(context)

    if chat.type == "private":
        kb = [
            [InlineKeyboardButton("Busco una casa", callback_data="menu_search")],
            [InlineKeyboardButton("Vendo una casa", callback_data="menu_sell")],
            [InlineKeyboardButton("Manuales / Herramientas útiles", callback_data="menu_manuals")],
            [InlineKeyboardButton("Contacto", callback_data="menu_contact")],
        ]
        txt = f"Hola {user.first_name or ''}! ¿Qué necesitas hoy?"
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    else:
        msg = (
            "Para usar el bot y enviar una casa, háblame en privado 👉 "
            f"@{bot_username} y escribe /start\n\n"
            "En este grupo usa comandos públicos como /madrid o /valencia."
        )
        if update.message:
            await update.message.reply_text(msg)
        else:
            await context.bot.send_message(chat.id, msg)

# ----------------------------
# Callback handler para el menú (gestiona privado/grupo)
# ----------------------------
async def callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    chat = q.message.chat
    user = q.from_user
    bot_username = await get_bot_username(context)

    # Si teclado en grupo => pedimos DM
    if chat.type in ("group", "supergroup", "channel"):
        warn = (
            "Esta función solo funciona en privado. Pulsa aquí 👉 "
            f"@{bot_username} y escribe /start"
        )
        try:
            await q.edit_message_text("Esta opción solo funciona en privado. Comprueba tu chat con el bot.")
        except Exception:
            pass
        try:
            await context.bot.send_message(user.id, warn)
        except Exception:
            try:
                await context.bot.send_message(chat.id, f"{user.first_name}, revisa tu chat privado con @{bot_username}.")
            except Exception:
                pass
        return

    # Si estamos en privado, procesar cada opción
    if data == "menu_search":
        # mostrar opciones de orden
        kb = [
            [InlineKeyboardButton("Top por rentabilidad", callback_data="search_sort_yield")],
            [InlineKeyboardButton("Top por precio (más barato)", callback_data="search_sort_price")],
            [InlineKeyboardButton("Buscar por ciudad", callback_data="search_by_city")],
            [InlineKeyboardButton("Volver", callback_data="menu_back")],
        ]
        await q.edit_message_text("Elige cómo quieres ver las propiedades:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "menu_sell":
        await q.edit_message_text("Perfecto. Empezamos. ¿En qué ciudad está el piso? (ej: Madrid)")
        return C_CITY

    if data == "menu_manuals":
        # Mostrar lista de manuales (por ahora uno)
        txt = "Manuales / Herramientas disponibles:\n\n"
        txt += f"- Calculadora de rentabilidad (PDF): {SAMPLE_PDF_URL}\n\n"
        txt += "Si quieres más dosieres, los añadiremos y te avisamos."
        await q.edit_message_text(txt)
        return

    if data == "menu_contact":
        await q.edit_message_text("Escribe el mensaje que quieres enviar al equipo (respuesta directa al admin).")
        return CONTACT_MSG

    if data == "menu_back":
        await q.edit_message_text("Menú principal. /start para volver.")
        return

    # search sub-options
    if data == "search_sort_yield":
        await q.edit_message_text("Buscando por rentabilidad (top 5)...")
        await send_listings_sorted(context, q.from_user.id, sort_by="yield")
        return

    if data == "search_sort_price":
        await q.edit_message_text("Buscando por precio (más barato, top 5)...")
        await send_listings_sorted(context, q.from_user.id, sort_by="price")
        return

    if data == "search_by_city":
        await q.edit_message_text("Escribe el nombre de la ciudad que quieres buscar (ej: Madrid).")
        # Next message will be handled by city_search_message
        context.user_data["awaiting_city_search"] = True
        return

    return

# ----------------------------
# Enviar listados: leer sheet, calcular yield y ordenar
# ----------------------------
def safe_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None

def parse_listing_row(row: List[str]) -> Dict[str, Any]:
    # Our header expected:
    # timestamp, chat_id, user, city, price, m2, rent_est, state, url, notes, photo_filename, contact
    data = {}
    data["timestamp"] = row[0] if len(row) > 0 else ""
    data["chat_id"] = row[1] if len(row) > 1 else ""
    data["user"] = row[2] if len(row) > 2 else ""
    data["city"] = row[3] if len(row) > 3 else ""
    data["price"] = safe_float(row[4]) if len(row) > 4 else None
    data["m2"] = safe_float(row[5]) if len(row) > 5 else None
    data["rent_est"] = safe_float(row[6]) if len(row) > 6 else None
    data["state"] = row[7] if len(row) > 7 else ""
    data["url"] = row[8] if len(row) > 8 else ""
    data["notes"] = row[9] if len(row) > 9 else ""
    data["photo"] = row[10] if len(row) > 10 else ""
    data["contact"] = row[11] if len(row) > 11 else ""
    # compute yield_net_approx = (rent_est * 12) / price * 100  (percent)
    if data["price"] and data["rent_est"] and data["price"] != 0:
        try:
            data["yield"] = round((data["rent_est"] * 12) / data["price"] * 100, 2)
        except Exception:
            data["yield"] = None
    else:
        data["yield"] = None
    return data

async def send_listings_sorted(context: ContextTypes.DEFAULT_TYPE, chat_id, sort_by="yield"):
    try:
        ws = ensure_sheet()
        rows = ws.get_all_records()
    except Exception as e:
        logger.exception("Error leyendo sheet para listados")
        await context.bot.send_message(chat_id, "No puedo leer las oportunidades ahora. Revisa configuración.")
        return

    listings = [parse_listing_row([
        r.get("timestamp",""),
        r.get("chat_id",""),
        r.get("user",""),
        r.get("city",""),
        r.get("price",""),
        r.get("m2",""),
        r.get("rent_est",""),
        r.get("state",""),
        r.get("url",""),
        r.get("notes",""),
        r.get("photo_filename",""),
        r.get("contact",""),
    ]) for r in rows]

    # filter out empty price and rent if sorting by yield
    if sort_by == "yield":
        listings = [l for l in listings if l.get("yield") is not None]
        listings.sort(key=lambda x: (x.get("yield") is None, -(x.get("yield") or 0)))
    elif sort_by == "price":
        listings = [l for l in listings if l.get("price") is not None]
        listings.sort(key=lambda x: (x.get("price") is None, x.get("price") or 0))
    elif sort_by == "city":
        listings.sort(key=lambda x: (x.get("city") or "").lower())

    top = listings[:5]
    if not top:
        await context.bot.send_message(chat_id, "No hay listados disponibles con esos criterios.")
        return

    for l in top:
        txt = f"🏠 {l.get('city') or '—'} · Precio: {l.get('price') or '—'}€ · m²: {l.get('m2') or '—'}\n"
        if l.get("yield") is not None:
            txt += f"📈 Yield aprox.: {l['yield']} %\n"
        if l.get("url"):
            txt += f"🔗 {l.get('url')}\n"
        if l.get("contact"):
            txt += f"📞 {l.get('contact')}\n"
        txt += f"Publicado: {l.get('timestamp')}\n"
        await context.bot.send_message(chat_id, txt)

# ----------------------------
# Conversational flow: "Vendo una casa" (en privado)
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
    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        fname = f"photo_{update.effective_user.id}_{int(datetime.utcnow().timestamp())}.jpg"
        path = f"./uploads/{fname}"
        os.makedirs("./uploads", exist_ok=True)
        try:
            await file.download_to_drive(path)
            context.user_data["photo"] = path
        except Exception:
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
            # Notificar admin principal (ADMIN_NOTIFY) del nuevo piso ofrecido
            try:
                target = ADMIN_NOTIFY
                # si es un id numérico
                if str(target).isdigit():
                    await context.bot.send_message(int(target), f"nuevo piso ofrecido · {s.get('city')} · {s.get('price')}")
                else:
                    await context.bot.send_message(target, f"nuevo piso ofrecido · {s.get('city')} · {s.get('price')}")
            except Exception:
                logger.exception("Error notificando admin via ADMIN_NOTIFY")
            # Notificar también los ADMIN_IDS si hay
            for a in ADMIN_IDS:
                try:
                    await context.bot.send_message(a, f"Nuevo piso ofrecido por {update.effective_user.full_name}: {s.get('city')} {s.get('price')}")
                except Exception:
                    logger.exception("Error notificando admin id")
        except Exception as e:
            logger.exception("Error guardando en sheet")
            await update.message.reply_text("Hubo un problema guardando el piso. Avisaré a un admin para que lo revise.")
            for a in ADMIN_IDS:
                try:
                    await context.bot.send_message(a, f"Error guardando envío: {e}")
                except Exception:
                    pass
    else:
        await update.message.reply_text("Cancelado.")
    return ConversationHandler.END

# ----------------------------
# Contacto: recoger texto y reenviar al admin
# ----------------------------
async def contact_message_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Entrada desde menu "Contacto" - ya handled returning CONTACT_MSG
    await update.message.reply_text("Escribe el mensaje que quieres que recibamos (responderemos por privado).")
    return CONTACT_MSG

async def contact_message_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    sender = update.effective_user
    try:
        target = ADMIN_NOTIFY
        forward_text = f"Mensaje de contacto de @{sender.username or sender.full_name} ({sender.id}):\n\n{text}"
        if str(target).isdigit():
            await context.bot.send_message(int(target), forward_text)
        else:
            await context.bot.send_message(target, forward_text)
        await update.message.reply_text("Mensaje enviado. Gracias, te responderemos por privado si procede.")
    except Exception:
        logger.exception("Error reenviando mensaje de contacto")
        await update.message.reply_text("No he podido reenviar el mensaje. Avisaré a los admins.")
        for a in ADMIN_IDS:
            try:
                await context.bot.send_message(a, f"Error reenviando mensaje contacto de {sender.id}: {text}")
            except Exception:
                pass
    return ConversationHandler.END

# ----------------------------
# Admin command: lista (últimos envíos)
# ----------------------------
async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("No autorizado")
    try:
        ws = ensure_sheet()
        rows = ws.get_all_records()[-10:]
        txt = "Últimos envíos:\n"
        for r in rows:
            txt += f"- {r.get('timestamp','')} | {r.get('user','')} | {r.get('city','')} | {r.get('price','')}€\n"
        await update.message.reply_text(txt)
    except Exception as e:
        logger.exception("Error en admin_list")
        await update.message.reply_text("Error leyendo la hoja. Revisa permisos y SPREADSHEET_ID.")

# ----------------------------
# City search if user typed a city after clicking "Buscar por ciudad"
# ----------------------------
async def city_search_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_city_search"):
        return
    city = update.message.text.strip().lower()
    context.user_data["awaiting_city_search"] = False
    # read sheet and filter by city
    try:
        ws = ensure_sheet()
        rows = ws.get_all_records()
    except Exception as e:
        logger.exception("Error leyendo sheet para búsqueda por ciudad")
        await update.message.reply_text("No puedo leer las oportunidades ahora. Revisa configuración.")
        return
    results = []
    for r in rows:
        if str(r.get("city","")).strip().lower() == city:
            results.append(parse_listing_row([
                r.get("timestamp",""),
                r.get("chat_id",""),
                r.get("user",""),
                r.get("city",""),
                r.get("price",""),
                r.get("m2",""),
                r.get("rent_est",""),
                r.get("state",""),
                r.get("url",""),
                r.get("notes",""),
                r.get("photo_filename",""),
                r.get("contact",""),
            ]))
    if not results:
        await update.message.reply_text(f"No he encontrado listados para {city.capitalize()}.")
        return
    # show up to 5
    for l in results[:5]:
        txt = f"🏠 {l.get('city')} · Precio: {l.get('price') or '—'}€ · m²: {l.get('m2') or '—'}\n"
        if l.get("yield") is not None:
            txt += f"📈 Yield aprox.: {l['yield']} %\n"
        if l.get("url"):
            txt += f"🔗 {l.get('url')}\n"
        await update.message.reply_text(txt)

# ----------------------------
# Welcome new members
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

        # Intentamos DM al usuario
        try:
            dm = (
                f"Hola {member.first_name or ''}! Bienvenido a Ready2R.\n\n"
                "Si quieres enviar un piso para análisis, háblame aquí 👉 "
                f"@{bot_username} y escribe /start"
            )
            await context.bot.send_message(member.id, dm)
        except Exception:
            pass

# ----------------------------
# Error handler
# ----------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception", exc_info=context.error)
    for a in ADMIN_IDS:
        try:
            await context.bot.send_message(a, f"Error en bot: {context.error}")
        except Exception:
            pass

# ----------------------------
# Build app and handlers
# ----------------------------
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Conversation handler for selling (entry via callback menu 'menu_sell')
    sell_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_menu, pattern=r"^menu_sell$")],
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

    # Conversation handler for contact messages (entry via callback menu 'menu_contact')
    contact_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_menu, pattern=r"^menu_contact$")],
        states={
            CONTACT_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, contact_message_save)],
        },
        fallbacks=[
            CommandHandler(
                "cancel",
                lambda u, c: (u.message.reply_text("Cancelado."), ConversationHandler.END)[1]
            )
        ],
        allow_reentry=True,
    )

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(sell_conv)
    app.add_handler(contact_conv)
    # CallbackQueryHandler para search subcommands y manual/menu handling
    app.add_handler(CallbackQueryHandler(callback_menu, pattern=r"^menu_|^search_|^menu_back$"))
    app.add_handler(CommandHandler("lista", admin_list))
    app.add_handler(MessageHandler(filters.Regex(r"^/[a-zA-ZñÑáéíóúÁÉÍÓÚ]+$"), city_command_handler := (lambda u,c: city_handler(u,c))))
    # handler para texto luego de "Buscar por ciudad"
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, city_search_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))
    app.add_error_handler(error_handler)

    return app

# small wrapper to satisfy city command usage
async def city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if not txt.startswith("/"):
        return
    city = txt[1:].strip()
    await update.message.reply_text(f"Buscando oportunidades en {city.capitalize()}... (próximamente).")

# ----------------------------
# Entrypoint
# ----------------------------
if __name__ == "__main__":
    logger.info("Starting Ready2R Bot (full menu)...")
    app = build_app()
    app.run_polling(poll_interval=3)
