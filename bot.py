import logging
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from urllib.parse import urljoin
from telegram.ext import MessageHandler, filters
import asyncio
import re

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
    ChatPermissions,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    JobQueue,
)
from telegram.helpers import mention_html
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# =======================
# CONFIG
# =======================
BOT_TOKEN = "7681582309:AAF8Zv0nNkV50LviL0gU1pusj8egDbE9_mw"   # <-- your token
BASE_URL = "https://beslenme.manas.edu.kg"
MENU_URL = f"{BASE_URL}/menu"
BISHKEK_TZ = pytz_timezone("Asia/Bishkek")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("manas_menu_bot")

# =======================
# UI (RU)
# =======================
LANG = "ru"
TXT = {
    "welcome": "Добро пожаловать! 👋\nВыберите, какое меню показать:",
    "today": "🍽️ Сегодня",
    "tomorrow": "🍱 Завтра",
    "dayafter": "🥘 Послезавтра",
    "no_today": "Меню на сегодня не найдено.",
    "no_tomorrow": "Меню на завтра не найдено.",
    "no_dayafter": "Меню на послезавтра не найдено.",
    "no_week": "Недельное меню не найдено.",
    "could_not_load": "❌ Не удалось загрузить меню. Попробуйте позже.",
    "kcal": "ккал",
}

# =======================
# CACHING
# =======================
CACHE_TTL = 600  # 10 minutes
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
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MenuBot/3.1)",
        "Accept-Language": "tr-TR,tr;q=0.9,ru;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }
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

from telegram import InputMediaPhoto  # make sure this import exists

def media_group_for(dishes: list[dict]):
    """Build a media group without captions to avoid repeating the first dish under the photos."""
    media = []
    for d in dishes:
        if d.get("img"):
            media.append(InputMediaPhoto(media=d["img"]))  # <-- no caption
    return media

# ======================= ADDED COMMANDS 
async def say(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    # If you reply to a message with /say -> bot re-sends that message as itself
    if msg.reply_to_message:
        await context.bot.copy_message(
            chat_id=msg.chat_id,
            from_chat_id=msg.chat_id,
            message_id=msg.reply_to_message.message_id,
        )
        return

    # Otherwise, repeat the text after the command
    text = " ".join(context.args) if context.args else None

    # If user sent "/say something" as a single message, try to strip the command
    if not text and msg.text:
        parts = msg.text.split(maxsplit=1)
        text = parts[1] if len(parts) > 1 else ""

    if text:
        # Telegram max message length guard
        if len(text) > 4096:
            text = text[:4090] + "…"
        await msg.reply_text(text)
    else:
        await msg.reply_text("Send `/say <text>` or reply to any message with `/say`.", parse_mode="Markdown")
        from telegram.ext import CommandHandler, filters
from telegram.helpers import escape_markdown

async def quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat_id = update.effective_chat.id

    # When replying to a message -> quote that message
    target = msg.reply_to_message
    if target:
        user = target.from_user
        # text to quote: prefer text, then caption (for photos, etc.)
        original = target.text or target.caption or ""
        if not original:
            original = "(media message)"

        # Build the "who" part
        if user and user.username:
            who_raw = f"@{user.username}"
            who = escape_markdown(who_raw, version=2)
        else:
            # fallback: inline mention of the user by id
            name = escape_markdown(user.full_name if user else "Someone", version=2)
            who = f"[{name}](tg://user?id={user.id})" if user else name

        # Escape original text for MarkdownV2 and truncate for safety
        body = escape_markdown(original, version=2)
        if len(body) > 3500:  # Telegram hard limit is 4096; keep headroom
            body = body[:3495] + "\\…"

        out = f"{who} said: {body}"
        await msg.reply_text(out, parse_mode="MarkdownV2", disable_web_page_preview=True)
        return

    # Not a reply: /quote <text>  -> quote the sender
    text = " ".join(context.args) if context.args else ""
    if not text:
        await msg.reply_text("Reply to a message with /quote, or use:\n/quote <text>")
        return

    sender = update.effective_user
    if sender and sender.username:
        who = escape_markdown(f"@{sender.username}", version=2)
    else:
        name = escape_markdown(sender.full_name if sender else "You", version=2)
        who = f"[{name}](tg://user?id={sender.id})" if sender else name

    body = escape_markdown(text, version=2)
    if len(body) > 3500:
        body = body[:3495] + "\\…"

    out = f"{who} said: {body}"
    await msg.reply_text(out, parse_mode="MarkdownV2", disable_web_page_preview=True)
# =======================
# MODERATION HELPERS (mute/unmute)
# =======================
DUR_RE = re.compile(r"^(\d+)([smhd])$", re.U)  # 10m, 2h, 1d

def parse_duration(s: str | None) -> timedelta | None:
    """Return a timedelta for inputs like '10m', '2h', '1d'; default 10m."""
    if not s:
        return timedelta(minutes=10)
    m = DUR_RE.match(s.lower())
    if not m:
        return timedelta(minutes=10)
    n, unit = int(m.group(1)), m.group(2)
    return {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
    }[unit]

def build_mute_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )

def build_unmute_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )

async def _resolve_target_from_message(msg):
    # Prefer reply
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user
    # Try TEXT_MENTION entity
    for ent in (msg.entities or []):
        if ent.type == "text_mention" and ent.user:
            return ent.user
    # Fallback: numeric ID as second arg (/mute 123456789 30m)
    parts = (msg.text or msg.caption or "").strip().split()
    if len(parts) >= 2:
        try:
            uid = int(parts[1])
            class _U:
                def __init__(self, id): self.id=id; self.full_name=f"ID {id}"
            return _U(uid)
        except Exception:
            pass
    return None

async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text("This command works only in groups.")
        return

    me = await context.bot.get_chat_member(chat.id, context.bot.id)
    if not (getattr(me, "can_restrict_members", False) or getattr(me, "status", "") in ("administrator","creator")):
        await msg.reply_text("I need the 'Restrict members' admin right.")
        return

    target = await _resolve_target_from_message(msg)
    if not target:
        await msg.reply_text("Reply to the user's message, or use a text mention (or numeric ID).")
        return

    parts = (msg.text or "").split()
    dur = parse_duration(parts[2] if len(parts) >= 3 else None)
    until = datetime.now(BISHKEK_TZ) + dur

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=target.id,
            permissions=build_mute_permissions(),
            until_date=until
        )
        await msg.reply_html(f"🔇 Muted {mention_html(target.id, getattr(target, 'full_name', 'user'))} until <b>{until.strftime('%H:%M, %d.%m')}</b>.")
    except BadRequest as e:
        await msg.reply_text(f"Failed to mute: {e.message or str(e)}")

async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text("This command works only in groups.")
        return

    me = await context.bot.get_chat_member(chat.id, context.bot.id)
    if not (getattr(me, "can_restrict_members", False) or getattr(me, "status", "") in ("administrator","creator")):
        await msg.reply_text("I need the 'Restrict members' admin right.")
        return

    target = await _resolve_target_from_message(msg)
    if not target:
        await msg.reply_text("Reply to the user's message, or use a text mention (or numeric ID).")
        return

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=target.id,
            permissions=build_unmute_permissions(),
            until_date=None
        )
        await msg.reply_html(f"🔊 Unmuted {mention_html(target.id, getattr(target, 'full_name', 'user'))}.")
    except BadRequest as e:
        await msg.reply_text(f"Failed to unmute: {e.message or str(e)}")
# TELEGRAM
# =======================
async def yemek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # command entrypoint: /yemek
    kb = [
        [InlineKeyboardButton(TXT["today"], callback_data="today")],
        [InlineKeyboardButton(TXT["tomorrow"], callback_data="tomorrow")],
        [InlineKeyboardButton(TXT["dayafter"], callback_data="dayafter")],
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
            await context.bot.send_message(
                chat_id=q.message.chat_id,
                text=format_day(date_key, dishes),
                parse_mode=ParseMode.MARKDOWN,
            )
            media = media_group_for(dishes)
            for i in range(0, len(media), 10):
                await context.bot.send_media_group(chat_id=q.message.chat_id, media=media[i:i+10])

# Optional debug
async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    html = fetch_menu_html()
    menu = parse_menu(html)
    days = len(menu)
    items = sum(len(v) for v in menu.values())
    imgs = sum(1 for v in menu.values() for d in v if d.get("img"))
    await update.message.reply_text(f"Days: {days}\nItems: {items}\nWith images: {imgs}")
SMS_REGEX = re.compile(r"^-sms\s+(\d{1,3})$")

async def sms_purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    text = msg.text or msg.caption or ""

    m = SMS_REGEX.match(text.strip())
    if not m:
        return

    n = int(m.group(1))
    # clamp to a sane limit (Telegram rate limits; 300 is already a lot)
    n = max(1, min(n, 300))

    # Permission hints
    if chat.type in ("group", "supergroup"):
        try:
            me = await context.bot.get_chat_member(chat.id, context.bot.id)
            if not (me.can_delete_messages or (getattr(me, "status", "") in ("creator", "administrator"))):
                await msg.reply_text("У меня нет права удалять сообщения в этом чате. Дайте право «Удалять сообщения».")
                return
        except Exception:
            pass
    else:
        # private chat: bot can only delete its own messages
        await msg.reply_text("В личном чате я могу удалять только свои сообщения.")
        # continue anyway; we’ll skip failures

    deleted = 0
    failures = 0

    # delete the command message itself last (or first, your choice)
    start_id = msg.message_id

    # Go backwards from the command message
    for i in range(1, n + 1):
        mid = start_id - i
        if mid <= 0:
            break
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=mid)
            deleted += 1
            # small delay to avoid 429 Too Many Requests
            await asyncio.sleep(0.03)
        except Exception:
            failures += 1
            # ignore messages we can’t delete (permissions, too old, etc.)
            await asyncio.sleep(0.01)

    # Optionally delete the command itself too
    try:
        await context.bot.delete_message(chat_id=chat.id, message_id=start_id)
    except Exception:
        pass

    # Send a transient status (then delete it so chat stays clean)
    try:
        status = await context.bot.send_message(
            chat.id, f"🧹 Удалено: {deleted} • Пропущено: {failures}"
        )
        await asyncio.sleep(2)
        await context.bot.delete_message(chat_id=chat.id, message_id=status.message_id)
    except Exception:
        pass

# =======================
# MAIN
# =======================
def main():
    scheduler = AsyncIOScheduler(timezone=BISHKEK_TZ)
    job_queue = JobQueue()
    job_queue.scheduler = scheduler

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .job_queue(job_queue)
        .build()
    )
    app.add_handler(
    CommandHandler(
        ["quote", "q"],
        quote,
        filters=filters.ChatType.PRIVATE | filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP
    )
)

    # use /yemek to open the menu
    app.add_handler(CommandHandler("yemek", yemek))
    # if you ALSO want /start, uncomment the next line:
    # app.add_handler(CommandHandler("start", yemek))

    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CommandHandler(["say", "echo"], say))
    # moderation
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CallbackQueryHandler(button))

    print("🤖 Bot is running... Press Ctrl+C to stop.")
    # Purge text trigger: "-sms 100"
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^-sms\s+\d{1,3}$"), sms_purge))

    print("🤖 Bot is running... Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()

