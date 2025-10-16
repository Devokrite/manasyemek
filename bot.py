import os
import re
import time
import asyncio
import requests
from io import BytesIO
from datetime import datetime, timedelta
from collections import OrderedDict
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, ChatPermissions
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

BOT_TOKEN = os.getenv("7681582309:AAF8Zv0nNkV50LviL0gU1pusj8egDbE9_mw")
MENU_URL = "https://beslenme.manas.edu.kg/menu"
TZ = ZoneInfo("Asia/Bishkek")

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}\s+\S+", re.UNICODE)
_cache = {"ts": 0, "html": None, "menu": None}


# ========================= MENU SCRAPER =========================
def fetch_menu_html():
    if time.time() - _cache["ts"] < 600 and _cache["html"]:
        return _cache["html"]
    headers = {"User-Agent": "Mozilla/5.0 (compatible; MenuBot/1.0)"}
    r = requests.get(MENU_URL, headers=headers, timeout=20)
    r.raise_for_status()
    _cache["ts"] = time.time()
    _cache["html"] = r.text
    _cache["menu"] = None
    return r.text


def parse_menu(html: str):
    if _cache["menu"] is not None:
        return _cache["menu"]
    soup = BeautifulSoup(html, "html.parser")
    result = OrderedDict()
    heads = soup.select("div.mbr-section-head")
    for head in heads:
        h5 = head.find("h5")
        if not h5:
            continue
        date_text = h5.get_text(strip=True)
        if not DATE_RE.match(date_text):
            continue
        row = head.find_next_sibling(lambda x: x.name == "div" and "row" in x.get("class", []))
        if not row:
            continue
        items = []
        for card in row.select("div.item.features-image"):
            img_tag = card.select_one(".item-img img")
            img_url = None
            if img_tag:
                src = img_tag.get("src") or img_tag.get("data-src") or ""
                img_url = "https://beslenme.manas.edu.kg" + src
            title_tag = (card.select_one(".item-content h5 a strong")
                         or card.select_one(".item-content h5 strong")
                         or card.select_one(".item-content h5"))
            name = title_tag.get_text(strip=True) if title_tag else None
            kcal = None
            kcal_tag = card.select_one(".item-content h6")
            if kcal_tag:
                m = re.search(r"Kalori:\s*(\d+)", kcal_tag.get_text(" ", strip=True))
                if m:
                    kcal = m.group(1)
            if name:
                items.append({"name": name, "kcal": kcal, "img": img_url})
        if items:
            result[date_text] = items
    _cache["menu"] = result
    return result


def format_day(date_key, dishes):
    lines = [f"*{date_key}*"]
    for d in dishes:
        if d["kcal"]:
            lines.append(f"• {d['name']} — _{d['kcal']} kcal_")
        else:
            lines.append(f"• {d['name']}")
    return "\n".join(lines)


def get_by_date_key(menu, dt):
    target = dt.strftime("%d.%m.%Y")
    for k, v in menu.items():
        if k.startswith(target):
            return k, v
    return None, None


def media_group_for(dishes):
    return [InputMediaPhoto(media=d["img"]) for d in dishes if d.get("img")]


# ========================= COMMANDS =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Merhaba! /yemek ile menüyü, /quote ile alıntı yapabilirsiniz.")


async def yemek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("🍽️ Bugün", callback_data="today")],
        [InlineKeyboardButton("🍱 Yarın", callback_data="tomorrow")],
        [InlineKeyboardButton("🥘 Ertesi Gün", callback_data="after")],
        [InlineKeyboardButton("📅 Haftalık", callback_data="week")]
    ]
    await update.message.reply_text("Menüyü görmek istediğiniz zamanı seçin:", reply_markup=InlineKeyboardMarkup(kb))


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        html = fetch_menu_html()
        menu = parse_menu(html)
    except Exception:
        await q.edit_message_text("❌ Menü yüklenemedi.")
        return

    now = datetime.now(TZ)
    choice = q.data

    async def send_day(k, v, none_msg):
        if not k:
            await q.edit_message_text(none_msg)
            return
        await q.edit_message_text(format_day(k, v), parse_mode=ParseMode.MARKDOWN)
        media = media_group_for(v)
        for i in range(0, len(media), 10):
            await context.bot.send_media_group(chat_id=q.message.chat_id, media=media[i:i + 10])

    if choice == "today":
        k, v = get_by_date_key(menu, now)
        await send_day(k, v, "Bugün için menü bulunamadı.")
    elif choice == "tomorrow":
        k, v = get_by_date_key(menu, now + timedelta(days=1))
        await send_day(k, v, "Yarın için menü bulunamadı.")
    elif choice == "after":
        k, v = get_by_date_key(menu, now + timedelta(days=2))
        await send_day(k, v, "Ertesi gün için menü bulunamadı.")
    elif choice == "week":
        if not menu:
            await q.edit_message_text("Haftalık menü bulunamadı.")
            return
        await q.edit_message_text("📅 Haftalık Menü")
        for date_key, dishes in menu.items():
            await context.bot.send_message(chat_id=q.message.chat_id, text=format_day(date_key, dishes), parse_mode=ParseMode.MARKDOWN)
            media = media_group_for(dishes)
            for i in range(0, len(media), 10):
                await context.bot.send_media_group(chat_id=q.message.chat_id, media=media[i:i + 10])


# ========================= SMS PURGE =========================
SMS_RE = re.compile(r"^-sms\s*(\d{1,3})$", re.IGNORECASE)

async def sms_purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    text = (msg.text or msg.caption or "").strip()
    m = SMS_RE.match(text)
    if not m:
        return
    n = max(1, min(int(m.group(1)), 300))
    me = await context.bot.get_chat_member(chat.id, context.bot.id)
    if not (me.status in ("administrator", "creator") and getattr(me, "can_delete_messages", True)):
        await msg.reply_text("❌ Yönetici olmam gerekiyor.")
        return
    deleted = 0
    for i in range(1, n + 1):
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=msg.message_id - i)
            deleted += 1
            await asyncio.sleep(0.03)
        except Exception:
            continue
    await msg.reply_text(f"🧹 {deleted} mesaj silindi.")


# ========================= MUTE / UNMUTE =========================
def parse_duration(duration_str: str) -> timedelta:
    match = re.match(r"(\d+)([smhd])", duration_str)
    if not match:
        return timedelta(minutes=5)
    value, unit = match.groups()
    value = int(value)
    if unit == "s":
        return timedelta(seconds=value)
    elif unit == "m":
        return timedelta(minutes=value)
    elif unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    return timedelta(minutes=5)


async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("🔇 Bir mesaja yanıt vererek susturmak istediğiniz kişiyi seçin.")
        return
    target = update.message.reply_to_message.from_user
    args = context.args
    duration = parse_duration(args[0]) if args else timedelta(minutes=10)
    until_date = datetime.now() + duration
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date
        )
        await update.message.reply_text(f"🤐 {target.first_name} {duration} süreyle susturuldu.")
    except Exception as e:
        await update.message.reply_text(f"❌ Hata: {e}")


async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("🔊 Bir mesaja yanıt vererek susturmayı kaldırmak istediğiniz kişiyi seçin.")
        return
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=True)
        )
        await update.message.reply_text(f"✅ {target.first_name} artık konuşabilir.")
    except Exception as e:
        await update.message.reply_text(f"❌ Hata: {e}")


# ========================= MAIN =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set it in Railway Variables or hardcode for testing.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("yemek", yemek))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(MessageHandler(filters.Regex(r"^-sms\s*\d{1,3}$"), sms_purge))
    app.run_polling()


if __name__ == "__main__":
    main()
