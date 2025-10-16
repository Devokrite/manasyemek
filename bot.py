# bot.py — Conflict-safe (retry) with /yemek, /quote, -sms
import os, sys, logging, re, time, asyncio
from collections import OrderedDict
from datetime import datetime, timedelta
from urllib.parse import urljoin
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag
from pytz import timezone as pytz_timezone
from deep_translator import GoogleTranslator
from PIL import Image, ImageDraw, ImageFont

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, BotCommand
)
from telegram.constants import ParseMode
from telegram.error import Conflict
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes,
    JobQueue, MessageHandler, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

VERSION = "v4.1-retry-on-conflict"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("manas_menu_bot")
log.info("Starting bot version %s", VERSION)

BOT_TOKEN = os.getenv("BOT_TOKEN") or "7681582309:AAF8Zv0nNkV50LviL0gU1pusj8egDbE9_mw"
BASE_URL = "https://beslenme.manas.edu.kg"
MENU_URL = f"{BASE_URL}/menu"
BISHKEK_TZ = pytz_timezone("Asia/Bishkek")

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

CACHE_TTL = 600
_cache = {"ts": 0.0, "parsed": None, "raw": None}
DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}\s+\S+", re.U)

def tr(text: str) -> str:
    try:
        return GoogleTranslator(source="auto", target="ru").translate(text)
    except Exception:
        return text

def fetch_menu_html() -> str:
    if time.time() - _cache["ts"] < CACHE_TTL and _cache["raw"]:
        return _cache["raw"]
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MenuBot/4.1)"}
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
            title_tag = (card.select_one(".item-content h5 a strong")
                         or card.select_one(".item-content h5 strong")
                         or card.select_one(".item-content h5"))
            name = title_tag.get_text(" ", strip=True) if isinstance(title_tag, Tag) else None
            kcal = None
            kcal_tag = card.select_one(".item-content h6")
            if isinstance(kcal_tag, Tag):
                m = re.search(r"Kalori:\s*(\d+)", kcal_tag.get_text(" ", strip=True))
                if m:
                    kcal = m.group(1)
            if name:
                items.append({"name": name, "name_ru": tr(name), "kcal": kcal, "img": img_url})
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

from telegram import InputMediaPhoto
def media_group_for(dishes: list[dict]):
    return [InputMediaPhoto(media=d["img"]) for d in dishes if d.get("img")]

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
        k, v = get_for_date(menu, now);      await send_day(k, v, "no_today")
    elif choice == "tomorrow":
        k, v = get_for_date(menu, now + timedelta(days=1));  await send_day(k, v, "no_tomorrow")
    elif choice == "dayafter":
        k, v = get_for_date(menu, now + timedelta(days=2));  await send_day(k, v, "no_dayafter")
    elif choice == "week":
        if not menu:
            await q.edit_message_text(TXT["no_week"]);  return
        await q.edit_message_text(TXT["weekly_header"])
        for date_key, dishes in menu.items():
            await context.bot.send_message(chat_id=q.message.chat_id, text=format_day(date_key, dishes), parse_mode=ParseMode.MARKDOWN)
            media = media_group_for(dishes)
            for i in range(0, len(media), 10):
                await context.bot.send_media_group(chat_id=q.message.chat_id, media=media[i:i+10])

SMS_REGEX = re.compile(r"^-sms\s*(\d{1,3})$", re.IGNORECASE)
async def sms_purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message; chat = update.effective_chat
    text = (msg.text or msg.caption or "").strip()
    m = SMS_REGEX.match(text)
    if not m: return
    n = max(1, min(int(m.group(1)), 300))
    if chat.type in ("group", "supergroup"):
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
        if not (me.status in ("administrator", "creator") and getattr(me, "can_delete_messages", True)):
            await msg.reply_text("Мне нужны права «Удалять сообщения» в этом чате.");  return
    else:
        await msg.reply_text("В личном чате я могу удалять только свои сообщения.")
    start_id = msg.message_id; deleted = 0; skipped = 0
    for i in range(1, n + 1):
        mid = start_id - i
        if mid <= 0: break
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=mid)
            deleted += 1; await asyncio.sleep(0.03)
        except Exception:
            skipped += 1; await asyncio.sleep(0.01)
    try: await context.bot.delete_message(chat_id=chat.id, message_id=start_id)
    except Exception: pass
    note = await context.bot.send_message(chat.id, f"🧹 Удалено: {deleted} • Пропущено: {skipped}")
    await asyncio.sleep(2); await note.delete()

# ---- /quote
async def quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("📌 Используй /quote как ответ на сообщение.");  return
    reply_msg = update.message.reply_to_message
    sender = reply_msg.from_user
    text = reply_msg.text or reply_msg.caption
    if not text:
        await update.message.reply_text("❌ Сообщение пустое или не поддерживается.");  return
    name = sender.full_name
    avatar = None
    try:
        photos = await context.bot.get_user_profile_photos(sender.id, limit=1)
        if photos.total_count > 0:
            file = await context.bot.get_file(photos.photos[0][0].file_id)
            resp = requests.get(file.file_path, timeout=15)
            avatar = Image.open(BytesIO(resp.content)).convert("RGBA")
    except Exception:
        avatar = None

    W, H = 800, 400
    bg = Image.new("RGB", (W, H), (40, 44, 52))
    draw = ImageDraw.Draw(bg)
    # Fallback to default font on servers
    try:
        font_name = ImageFont.truetype("DejaVuSans-Bold.ttf", 30)
        font_text = ImageFont.truetype("DejaVuSans.ttf", 24)
    except Exception:
        font_name = ImageFont.load_default(); font_text = ImageFont.load_default()

    if avatar:
        avatar = avatar.resize((100, 100))
        mask = Image.new("L", (100, 100), 0); ImageDraw.Draw(mask).ellipse((0,0,100,100), fill=255)
        bg.paste(avatar, (30, 30), mask)

    import textwrap
    draw.text((150, 50), name, fill=(255,255,255), font=font_name)
    wrapped = textwrap.fill(text, width=45)
    draw.text((150, 100), wrapped, fill=(220,220,220), font=font_text)

    out = BytesIO(); bg.save(out, format="PNG"); out.seek(0)
    await update.message.reply_photo(photo=out, caption=f"💬 Цитата от {name}")

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    html = fetch_menu_html(); menu = parse_menu(html)
    days = len(menu); items = sum(len(v) for v in menu.values()); imgs = sum(1 for v in menu.values() for d in v if d.get("img"))
    await update.message.reply_text(f"Days: {days}\nItems: {items}\nWith images: {imgs}")

async def post_init(app):
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.bot.set_my_commands([
        BotCommand("yemek", "Показать меню"),
        BotCommand("quote", "Создать цитату из сообщения"),
        BotCommand("debug", "Статистика парсера"),
    ])

async def on_error(update: object, context):
    # Log but DO NOT exit; run_polling loop below will handle Conflict backoff
    logging.exception("Error: %s", context.error)

def build_app():
    scheduler = AsyncIOScheduler(timezone=BISHKEK_TZ)
    job_queue = JobQueue(); job_queue.scheduler = scheduler
    app = (ApplicationBuilder().token(BOT_TOKEN).job_queue(job_queue).post_init(post_init).build())
    app.add_handler(CommandHandler("yemek", yemek))
    app.add_handler(CommandHandler("quote", quote))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^-sms\s*\d{1,3}$"), sms_purge))
    app.add_error_handler(on_error)
    return app

def main():
    if not BOT_TOKEN or BOT_TOKEN == "REPLACE_ME_WITH_BOTFATHER_TOKEN":
        raise RuntimeError("BOT_TOKEN is missing.")
    # Retry loop: if Conflict occurs, sleep & retry polling
    backoff = 5
    while True:
        app = build_app()
        try:
            log.info("🤖 Bot is running...")
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        except Conflict as e:
            log.error("Conflict detected (%s). Retrying in %ss …", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)  # exponential backoff up to 60s
            continue
        except Exception as e:
            log.exception("Fatal error: %s", e)
            time.sleep(5)
            continue
        # Normal stop (rare in Railway); break loop
        break

if __name__ == "__main__":
    main()
