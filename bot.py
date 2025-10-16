# bot.py (Railway-friendly, conflict-proof, with /yemek, dayafter + week, purge command)
import os
import sys
import logging
import re
import time
import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag
from pytz import timezone as pytz_timezone
from deep_translator import GoogleTranslator

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    BotCommand,
)
from telegram.constants import ParseMode
from telegram.error import Conflict
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    JobQueue,
    MessageHandler,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# =======================
# VERSION & LOGGING
# =======================
VERSION = "v3.3-conflict-guard"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("manas_menu_bot")
log.info("Starting bot version %s", VERSION)

# =======================
# CONFIG
# =======================
BOT_TOKEN = os.getenv("BOT_TOKEN") or "7681582309:AAF8Zv0nNkV50LviL0gU1pusj8egDbE9_mw"
BASE_URL = "https://beslenme.manas.edu.kg"
MENU_URL = f"{BASE_URL}/menu"
BISHKEK_TZ = pytz_timezone("Asia/Bishkek")

# =======================
# UI (RU)
# =======================
TXT = {
    "welcome": "Добро пожаловать! 👋\nВыберите, какое меню показать:",
    "today": "🍽️ Сегодня",
    "tomorrow": "🍱 Завтра",
    "dayafter": "🥘 Послезавтра",
    "week": "📅 Неделя",
    "no_today": "Меню на сегодня не найдено.",
    "no_tomorrow": "Меню на завтра не найдено.",
    "no_dayafter": "Меню на послезавтра не найдено.",
    "no_week": "Недельное меню не найдено.",
    "weekly_header": "📅 Меню на неделю (фото блюд ниже)",
    "could_not_load": "❌ Не удалось загрузить меню. Попробуйте позже.",
    "kcal": "ккал",
}

# =======================
# CACHING
# =======================
CACHE_TTL = 600
_cache = {"ts": 0.0, "parsed": None, "raw": None}

# =======================
# HELPERS
# =======================
DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}\s+\S+", re.U)

def tr(text: str) -> str:
    try:
        return GoogleTranslator(source="auto", target="ru").translate(text)
    except Exception:
        return text

def fetch_menu_html() -> str:
    if time.time() - _cache["ts"] < CACHE_TTL and _cache["raw"]:
        return _cache["raw"]
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MenuBot/3.3)"}
    r = requests.get(MENU_URL, headers=headers, timeout=15)
    r.raise_for_status()
    _cache["raw"] = r.text
    _cache["parsed"] = None
    _cache["ts"] = time.time()
    return r.text

def parse_menu(html: str):
    if _cache["parsed"] is not None and _cache["raw"] == html:
        return _cache["parsed"]
    soup = BeautifulSoup(html, "html.parser")
    result = OrderedDict()
    heads = soup.select("div.mbr-section-head")
    for head in heads:
        h5 = head.find("h5")
        if not isinstance(h5, Tag):
            continue
        date_text = h5.get_text(" ", strip=True)
        if not DATE_RE.match(date_text):
            continue
        row = head.find_next_sibling(lambda x: isinstance(x, Tag) and x.name == "div" and "row" in x.get("class", []))
        if not row:
            continue
        items = []
        for card in row.select("div.item.features-image"):
            img_tag = card.select_one(".item-img img")
            img_url = None
            if isinstance(img_tag, Tag):
                src = img_tag.get("src") or img_tag.get("data-src") or ""
                img_url = urljoin(BASE_URL, src)
            title_tag = (
                card.select_one(".item-content h5 a strong")
                or card.select_one(".item-content h5 strong")
                or card.select_one(".item-content h5")
            )
            name = title_tag.get_text(" ", strip=True) if isinstance(title_tag, Tag) else None
            kcal = None
            kcal_tag = card.select_one(".item-content h6")
            if isinstance(kcal_tag, Tag):
                m = re.search(r"Kalori:\s*(\d+)", kcal_tag.get_text(" ", strip=True))
                if m:
                    kcal = m.group(1)
            if name:
                items.append({
                    "name": name,
                    "name_ru": tr(name),
                    "kcal": kcal,
                    "img": img_url,
                })
        if items:
            result[date_text] = items
    _cache["parsed"] = result
    return result

def format_day(date_key: str, dishes: list[dict]) -> str:
    lines = [f"*{date_key}*"]
    for d in dishes:
        nm = d["name_ru"] or d["name"]
        if d["kcal"]:
            lines.append(f"• {nm} — _{d['kcal']} {TXT['kcal']}_")
        else:
            lines.append(f"• {nm}")
    return "\n".join(lines)

def get_for_date(menu, dt: datetime):
    target = dt.strftime("%d.%m.%Y")
    for k, v in menu.items():
        if k.startswith(target):
            return k, v
    return None, None

def media_group_for(dishes: list[dict]):
    media = []
    for d in dishes:
        if d.get("img"):
            media.append(InputMediaPhoto(media=d["img"]))
    return media

# =======================
# COMMANDS & HANDLERS
# =======================
async def yemek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton(TXT["today"], callback_data="today")],
        [InlineKeyboardButton(TXT["tomorrow"], callback_data="tomorrow")],
        [InlineKeyboardButton(TXT["dayafter"], callback_data="dayafter")],
        [InlineKeyboardButton(TXT["week"], callback_data="week")],
    ]
    await update.message.reply_text(TXT["welcome"], reply_markup=InlineKeyboardMarkup(kb))

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        html = fetch_menu_html()
        menu = parse_menu(html)
    except Exception:
        log.exception("Fetch/parse failed")
        await q.edit_message_text(TXT["could_not_load"])
        return
    now = datetime.now(BISHKEK_TZ)
    choice = q.data

    async def send_day(k, v, no_text_key):
        if not k:
            await q.edit_message_text(TXT[no_text_key])
            return
        await q.edit_message_text(format_day(k, v), parse_mode=ParseMode.MARKDOWN)
        media = media_group_for(v)
        for i in range(0, len(media), 10):
            await context.bot.send_media_group(chat_id=q.message.chat_id, media=media[i:i+10])

    if choice == "today":
        k, v = get_for_date(menu, now)
        await send_day(k, v, "no_today")
    elif choice == "tomorrow":
        k, v = get_for_date(menu, now + timedelta(days=1))
        await send_day(k, v, "no_tomorrow")
    elif choice == "dayafter":
        k, v = get_for_date(menu, now + timedelta(days=2))
        await send_day(k, v, "no_dayafter")
    elif choice == "week":
        if not menu:
            await q.edit_message_text(TXT["no_week"])
            return
        await q.edit_message_text(TXT["weekly_header"])
        for date_key, dishes in menu.items():
            await context.bot.send_message(chat_id=q.message.chat_id, text=format_day(date_key, dishes), parse_mode=ParseMode.MARKDOWN)
            media = media_group_for(dishes)
            for i in range(0, len(media), 10):
                await context.bot.send_media_group(chat_id=q.message.chat_id, media=media[i:i+10])

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    html = fetch_menu_html()
    menu = parse_menu(html)
    days = len(menu)
    items = sum(len(v) for v in menu.values())
    imgs = sum(1 for v in menu.values() for d in v if d.get("img"))
    await update.message.reply_text(f"Days: {days}\\nItems: {items}\\nWith images: {imgs}")

SMS_REGEX = re.compile(r"^-sms\\s*(\\d{1,3})$", re.IGNORECASE)

async def sms_purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    text = (msg.text or msg.caption or "").strip()
    m = SMS_REGEX.match(text)
    if not m:
        return
    n = int(m.group(1))
    n = max(1, min(n, 300))
    if chat.type in ("group", "supergroup"):
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
        if not (me.status in ("administrator", "creator") and getattr(me, "can_delete_messages", True)):
            await msg.reply_text("Мне нужны права «Удалять сообщения» в этом чате.")
            return
    else:
        await msg.reply_text("В личном чате я могу удалять только свои сообщения.")
    start_id = msg.message_id
    deleted = 0
    skipped = 0
    for i in range(1, n + 1):
        mid = start_id - i
        if mid <= 0:
            break
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=mid)
            deleted += 1
            await asyncio.sleep(0.03)
        except Exception:
            skipped += 1
            await asyncio.sleep(0.01)
    try:
        await context.bot.delete_message(chat_id=chat.id, message_id=start_id)
    except Exception:
        pass
    try:
        s = await context.bot.send_message(chat.id, f"🧹 Удалено: {deleted} • Пропущено: {skipped}")
        await asyncio.sleep(2)
        await context.bot.delete_message(chat_id=chat.id, message_id=s.message_id)
    except Exception:
        pass

async def post_init(app):
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.bot.set_my_commands([
        BotCommand("yemek", "Показать меню: сегодня/завтра/послезавтра/неделя"),
        BotCommand("debug", "Статистика парсера"),
    ], language_code="ru")
    await app.bot.set_my_commands([
        BotCommand("yemek", "Show menu: today/tomorrow/day after/week"),
        BotCommand("debug", "Parser stats"),
    ])

async def on_error(update: object, context):
    err = context.error
    logging.exception("Handler error: %s", err)
    if isinstance(err, Conflict):
        await asyncio.sleep(1)
        sys.exit(0)

def main():
    if not BOT_TOKEN or BOT_TOKEN == "REPLACE_ME_WITH_BOTFATHER_TOKEN":
        raise RuntimeError("BOT_TOKEN is missing.")
    scheduler = AsyncIOScheduler(timezone=BISHKEK_TZ)
    job_queue = JobQueue()
    job_queue.scheduler = scheduler
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .job_queue(job_queue)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("yemek", yemek))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^-sms\\s*\\d{1,3}$"), sms_purge))
    app.add_error_handler(on_error)
    log.info("🤖 Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
