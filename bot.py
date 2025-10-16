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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
)

BOT_TOKEN = os.getenv("7681582309:AAF8Zv0nNkV50LviL0gU1pusj8egDbE9_mw")
MENU_URL = "https://beslenme.manas.edu.kg/menu"
TZ = ZoneInfo("Asia/Bishkek")

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}\s+\S+", re.UNICODE)
_cache = {"ts": 0, "html": None, "menu": None}

# ========== MENU SCRAPER ==========
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

# ========== COMMANDS ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Merhaba! Menüyü görmek için /yemek yazabilirsin veya bir mesajı alıntılamak için /quote kullanabilirsin.")

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
        await q.edit_message_text("❌ Menü yüklenemedi. Lütfen tekrar deneyin.")
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
            await context.bot.send_media_group(chat_id=q.message.chat_id, media=media[i:i+10])

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
                await context.bot.send_media_group(chat_id=q.message.chat_id, media=media[i:i+10])

# ========== SMS PURGE ==========

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
        await msg.reply_text("❌ Mesajları silebilmem için yönetici olmam gerekiyor.")
        return
    start_id = msg.message_id
    deleted = 0
    for i in range(1, n + 1):
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=start_id - i)
            deleted += 1
            await asyncio.sleep(0.03)
        except Exception:
            continue
    await msg.reply_text(f"🧹 {deleted} mesaj silindi.")

# ========== QUOTE ==========

async def quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("📌 Bir mesaja yanıt vererek /quote komutunu kullanın.")
        return

    reply_msg = update.message.reply_to_message
    sender = reply_msg.from_user
    text = reply_msg.text or reply_msg.caption
    if not text:
        await update.message.reply_text("❌ Bu mesajda alıntılanacak metin yok.")
        return

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

    W = 900
    PADDING = 40
    BUBBLE_PADDING = 30
    PROFILE_SIZE = 100
    LINE_SPACING = 10

    bg = Image.new("RGB", (W, 400), (235, 235, 235))
    draw = ImageDraw.Draw(bg)

    try:
        font_name = ImageFont.truetype("DejaVuSans-Bold.ttf", 32)
        font_text = ImageFont.truetype("DejaVuSans.ttf", 28)
    except Exception:
        font_name = ImageFont.load_default()
        font_text = ImageFont.load_default()

    if avatar:
        avatar = avatar.resize((PROFILE_SIZE, PROFILE_SIZE))
        mask = Image.new("L", (PROFILE_SIZE, PROFILE_SIZE), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, PROFILE_SIZE, PROFILE_SIZE), fill=255)
        bg.paste(avatar, (PADDING, PADDING), mask)

    name_x = PADDING + PROFILE_SIZE + 20
    name_y = PADDING + 5
    draw.text((name_x, name_y), name, fill="#0088cc", font=font_name)

    text_width = W - name_x - PADDING - BUBBLE_PADDING
    lines = []
    words = text.split()
    current = ""
    for word in words:
        test_line = (current + " " + word).strip()
        if draw.textlength(test_line, font=font_text) <= text_width:
            current = test_line
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    text_height = sum(font_text.getbbox(line)[3] for line in lines) + (len(lines) - 1) * LINE_SPACING

    bubble_x = name_x
    bubble_y = name_y + 45
    bubble_w = text_width + BUBBLE_PADDING * 2
    bubble_h = text_height + BUBBLE_PADDING * 2

    shadow_offset = 4
    shadow_color = (180, 180, 180)
    draw.rounded_rectangle(
        (bubble_x + shadow_offset, bubble_y + shadow_offset, bubble_x + bubble_w + shadow_offset, bubble_y + bubble_h + shadow_offset),
        radius=25, fill=shadow_color
    )
    draw.rounded_rectangle(
        (bubble_x, bubble_y, bubble_x + bubble_w, bubble_y + bubble_h),
        radius=25, fill="white"
    )

    ty = bubble_y + BUBBLE_PADDING
    for line in lines:
        draw.text((bubble_x + BUBBLE_PADDING, ty), line, fill=(0, 0, 0), font=font_text)
        ty += font_text.getbbox(line)[3] + LINE_SPACING

    height_needed = ty + PADDING
    if height_needed < bg.height:
        bg = bg.crop((0, 0, W, height_needed))

    out = BytesIO()
    bg.save(out, format="PNG")
    out.seek(0)
    await update.message.reply_photo(photo=out, caption=f"💬 {name} dedi ki:")

# ========== MAIN ==========

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("yemek", yemek))
    app.add_handler(CommandHandler("quote", quote))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.Regex(r"^-sms\s*\d{1,3}$"), sms_purge))
    app.run_polling()

if __name__ == "__main__":
    main()
