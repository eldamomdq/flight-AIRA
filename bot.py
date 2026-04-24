import os
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

def clean(val: str | None) -> str:
    """Elimina comillas y espacios que Railway puede agregar."""
    if not val:
        return ""
    return val.strip().strip('"').strip("'").strip()

# --- Config ---
TELEGRAM_TOKEN = clean(os.environ.get("TELEGRAM_TOKEN"))
CHAT_ID = clean(os.environ.get("CHAT_ID"))
SERPAPI_KEY = clean(os.environ.get("SERPAPI_KEY"))
CHECK_INTERVAL_HOURS = int(clean(os.environ.get("CHECK_INTERVAL_HOURS", "6")))
PRICE_THRESHOLD_USD = float(clean(os.environ.get("PRICE_THRESHOLD_USD", "300")))

# DEBUG — aparece en los logs de Railway
print(f"DEBUG TOKEN: '{TELEGRAM_TOKEN[:10]}...'" if TELEGRAM_TOKEN else "DEBUG TOKEN: VACÍO")
print(f"DEBUG CHAT_ID: '{CHAT_ID}'")
print(f"DEBUG TOKEN LEN: {len(TELEGRAM_TOKEN)}")

# Orígenes Argentina
ORIGINS = ["EZE", "AEP"]  # Podés agregar COR, MDZ, ROS si querés más cobertura

# Destinos disponibles
AVAILABLE_DESTINATIONS = {
    "🇧🇷 São Paulo": "GRU",
    "🇧🇷 Río de Janeiro": "GIG",
    "🇨🇱 Santiago": "SCL",
    "🇺🇾 Montevideo": "MVD",
    "🇵🇾 Asunción": "ASU",
    "🇵🇪 Lima": "LIM",
    "🇨🇴 Bogotá": "BOG",
    "🇲🇽 Ciudad de México": "MEX",
    "🇺🇸 Miami": "MIA",
    "🇺🇸 Nueva York": "JFK",
    "🇪🇸 Madrid": "MAD",
    "🇮🇹 Roma": "FCO",
    "🇬🇧 Londres": "LHR",
    "🇫🇷 París": "CDG",
    "🇩🇪 Frankfurt": "FRA",
    "🇹🇭 Bangkok": "BKK",
    "🇯🇵 Tokio": "NRT",
    "🇦🇺 Sydney": "SYD",
    "🇨🇦 Toronto": "YYZ",
    "🇵🇹 Lisboa": "LIS",
    "🇵🇱 Varsovia": "WAW",
    "🇵🇱 Cracovia": "KRK",
}

SELECTING_DESTINATIONS = 1

user_destinations: set = set()
seen_deals: dict = {}


def get_dates():
    today = datetime.now()
    # 4 fechas espaciadas en los próximos 60 días
    return [(today + timedelta(days=d)).strftime("%Y-%m-%d") for d in [7, 20, 35, 50]]


async def search_google_flights(origin: str, destination: str, date: str) -> list:
    """Llama a SerpAPI Google Flights y devuelve vuelos con precio."""
    url = "https://serpapi.com/search"
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": date,
        "currency": "USD",
        "hl": "es",
        "api_key": SERPAPI_KEY,
        "type": "2",  # 1=ida y vuelta, 2=solo ida
    }
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            return data.get("best_flights", []) + data.get("other_flights", [])
        except Exception as e:
            logger.error(f"SerpAPI error {origin}→{destination} {date}: {e}")
            return []


async def find_deals() -> list:
    deals = []
    dates = get_dates()

    for destination in user_destinations:
        for origin in ORIGINS:
            for date in dates:
                flights = await search_google_flights(origin, destination, date)
                for flight in flights:
                    price = flight.get("price")
                    if price and float(price) <= PRICE_THRESHOLD_USD:
                        deal_key = f"{origin}-{destination}-{date}-{price}"
                        if deal_key not in seen_deals:
                            seen_deals[deal_key] = True
                            # Extraer info del primer segmento
                            legs = flight.get("flights", [{}])
                            first = legs[0] if legs else {}
                            booking_token = flight.get("booking_token", "")
                            if booking_token:
                                buy_url = f"https://www.google.com/flights?hl=es#flt={origin}.{destination}.{date};c:USD;e:1;sd:1;t:f;tt:o&booking_token={booking_token}"
                            else:
                                buy_url = f"https://www.google.com/flights?hl=es#flt={origin}.{destination}.{date};c:USD;e:1;sd:1;t:f;tt:o"
                            deals.append({
                                "origin": origin,
                                "destination": destination,
                                "date": date,
                                "price": float(price),
                                "airline": first.get("airline", "?"),
                                "departure": first.get("departure_airport", {}).get("time", ""),
                                "duration": flight.get("total_duration", 0),
                                "stops": len(legs) - 1,
                                "buy_url": buy_url,
                            })
    return deals


def format_deals(deals: list) -> str | None:
    if not deals:
        return None
    deals.sort(key=lambda x: x["price"])
    msg = "✈️ *¡VUELOS BARATOS ENCONTRADOS!*\n\n"
    for d in deals:
        stops = "Directo" if d["stops"] == 0 else f"{d['stops']} escala(s)"
        hrs = d["duration"] // 60
        mins = d["duration"] % 60
        duracion = f"{hrs}h {mins}m" if d["duration"] else "—"
        msg += (
            f"🟢 *{d['origin']} → {d['destination']}*\n"
            f"   📅 {d['date']}  🕐 {d['departure']}\n"
            f"   💵 *USD {d['price']:.0f}*  •  {stops}  •  {duracion}\n"
            f"   ✈️ {d['airline']}\n"
            f"   [🛒 Ver y comprar en Google Flights]({d['buy_url']})\n\n"
        )
    msg += f"_Actualizado: {datetime.now().strftime('%d/%m %H:%M')}_"
    return msg


# ── Handlers ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 ¡Hola! Soy tu bot de alertas de vuelos baratos desde Argentina 🇦🇷\n\n"
        "Comandos:\n"
        "• /destinos — Elegir destinos\n"
        "• /buscar — Buscar ahora\n"
        "• /estado — Ver config actual\n"
        "• /umbral 250 — Cambiar precio máximo (USD)\n"
        "• /ayuda — Más info"
    )


async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ℹ️ *Configuración:*\n"
        f"• Búsqueda automática cada *{CHECK_INTERVAL_HOURS}hs*\n"
        f"• Precio máximo: *USD {PRICE_THRESHOLD_USD:.0f}*\n"
        f"• Orígenes: {', '.join(ORIGINS)}\n"
        f"• Ventana: próximos 60 días\n\n"
        f"Usá /destinos para seleccionar destinos.",
        parse_mode="Markdown"
    )


async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_destinations:
        await update.message.reply_text("⚠️ No tenés destinos. Usá /destinos para agregar.")
        return
    names = [k for k, v in AVAILABLE_DESTINATIONS.items() if v in user_destinations]
    await update.message.reply_text(
        f"📊 *Estado:*\n\n"
        f"✅ Destinos:\n" + "\n".join(f"  • {n}" for n in names) +
        f"\n\n💵 Umbral: USD {PRICE_THRESHOLD_USD:.0f}\n"
        f"⏰ Búsqueda cada {CHECK_INTERVAL_HOURS}hs",
        parse_mode="Markdown"
    )


async def umbral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PRICE_THRESHOLD_USD
    try:
        PRICE_THRESHOLD_USD = float(context.args[0])
        await update.message.reply_text(f"✅ Umbral actualizado a USD {PRICE_THRESHOLD_USD:.0f}")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Uso correcto: /umbral 250")


async def destinos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✈️ *Seleccioná los destinos a monitorear:*\n✅ = activo",
        reply_markup=build_destinations_keyboard(),
        parse_mode="Markdown"
    )
    return SELECTING_DESTINATIONS


def build_destinations_keyboard():
    keyboard, row = [], []
    for i, (name, code) in enumerate(AVAILABLE_DESTINATIONS.items()):
        label = ("✅ " if code in user_destinations else "") + name
        row.append(InlineKeyboardButton(label, callback_data=f"dest_{code}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("💾 Guardar", callback_data="dest_save")])
    return InlineKeyboardMarkup(keyboard)


async def destinos_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "dest_save":
        count = len(user_destinations)
        await query.edit_message_text(
            f"✅ ¡Guardado! Monitoreando *{count}* destino(s).\nUsá /buscar para probar ahora.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    code = query.data.replace("dest_", "")
    if code in user_destinations:
        user_destinations.discard(code)
    else:
        user_destinations.add(code)

    await query.edit_message_reply_markup(reply_markup=build_destinations_keyboard())
    return SELECTING_DESTINATIONS


async def buscar_ahora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_destinations:
        await update.message.reply_text("⚠️ Primero elegí destinos con /destinos")
        return
    msg = await update.message.reply_text("🔍 Consultando Google Flights... un momento.")
    deals = await find_deals()
    result = format_deals(deals)
    if result:
        await msg.edit_text(result, parse_mode="Markdown")
    else:
        await msg.edit_text(
            f"😔 Nada por debajo de USD {PRICE_THRESHOLD_USD:.0f} hoy.\n"
            f"Probá subir el umbral con /umbral [monto]."
        )


async def auto_check(app: Application):
    if not user_destinations:
        return
    logger.info("Búsqueda automática...")
    deals = await find_deals()
    result = format_deals(deals)
    if result:
        await app.bot.send_message(chat_id=CHAT_ID, text=result, parse_mode="Markdown")
        logger.info(f"Alertas enviadas: {len(deals)} deal(s)")
    else:
        logger.info("Sin ofertas nuevas.")


# ── Main ──────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("destinos", destinos_command)],
        states={SELECTING_DESTINATIONS: [CallbackQueryHandler(destinos_callback, pattern="^dest_")]},
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("estado", estado))
    app.add_handler(CommandHandler("umbral", umbral))
    app.add_handler(CommandHandler("buscar", buscar_ahora))
    app.add_handler(conv)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        auto_check, "interval",
        hours=CHECK_INTERVAL_HOURS,
        args=[app],
        next_run_time=datetime.now() + timedelta(minutes=2)
    )
    scheduler.start()

    logger.info(f"✅ Bot activo — búsqueda cada {CHECK_INTERVAL_HOURS}hs")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
