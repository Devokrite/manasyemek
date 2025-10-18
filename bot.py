import asyncio
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag
from deep_translator import GoogleTranslator
from pytz import timezone as pytz_timezone
from PIL import Image, ImageDraw, ImageFont, ImageOps  # <-- Image included

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ChatPermissions,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest
from telegram.helpers import mention_html
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
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import Update
from telegram.ext import ContextTypes
# put with your /quote code imports
from pathlib import Path
from PIL import ImageFont

def _pick_font(size: int):
    # 1) Use Pillow’s bundled DejaVu (has Cyrillic)
    try:
        pil_font = Path(ImageFont.__file__).parent / "fonts" / "DejaVuSans.ttf"
        return ImageFont.truetype(str(pil_font), size=size)
    except Exception:
        pass

    # 2) Try common system fonts if available
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size=size)
            except Exception:
                continue

    # 3) Last resort (won’t render Cyrillic perfectly but avoids crash)
    return ImageFont.load_default()

# =======================
# CONFIG
# =======================
BOT_TOKEN = "7681582309:AAF8Zv0nNkV50LviL0gU1pusj8egDbE9_mw"   # <-- your token
BASE_URL = "https://beslenme.manas.edu.kg"
MENU_URL = f"{BASE_URL}/menu"
BISHKEK_TZ = pytz_timezone("Asia/Bishkek")
OWNER_IDS = {838410534}

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

#
# -------------------------------
# /quote command (PTB v20+)
# -------------------------------
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import Update
from telegram.ext import ContextTypes

# If you have a TTF you like, put its path here; otherwise we’ll fall back.
PRIMARY_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"  # change if needed

def _load_font(size: int):
    try:
        return ImageFont.truetype(PRIMARY_FONT_PATH, size=size)
    except Exception:
        return ImageFont.load_default()

async def _get_user_avatar(bot, user_id: int, size: int = 160) -> Image.Image:
    """Fetch user's avatar; return a circular cropped PIL image (RGBA)."""
    try:
        photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
        if photos.total_count > 0:
            file = await bot.get_file(photos.photos[0][-1].file_id)
            b = await file.download_as_bytearray()
            img = Image.open(BytesIO(b)).convert("RGBA")
        else:
            raise RuntimeError("no photo")
    except Exception:
        # Placeholder avatar (gray circle)
        img = Image.new("RGBA", (size, size), (200, 200, 200, 255))
        d = ImageDraw.Draw(img)
        d.ellipse((0, 0, size-1, size-1), fill=(180, 180, 180, 255))

    # Square crop -> resize -> circle mask
    img = ImageOps.fit(img, (size, size), method=Image.LANCZOS, centering=(0.5, 0.5))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    img.putalpha(mask)
    return img

def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Simple greedy wrapper so the text fits the bubble width."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines) if lines else ""

async def quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    bot = context.bot

    # Determine target message and author
    target = message.reply_to_message
    if target and (target.text or target.caption):
        text_to_quote = target.text or target.caption
        author = target.from_user
    else:
        # Use args as text if not replying
        text_to_quote = " ".join(context.args).strip()
        author = message.from_user

    if not text_to_quote:
        await message.reply_text("Reply to a message with /quote or use `/quote your text`.", parse_mode=None)
        return

    # Canvas settings
    W, H = 900, 450
    PADDING = 36
    AVATAR_SIZE = 160
    BG = (18, 18, 18, 255)        # background
    BUBBLE = (28, 28, 28, 255)    # quote bubble
    WHITE = (245, 245, 245, 255)
    NAME = (170, 152, 255, 255)   # a soft purple
    SUBTLE = (200, 200, 200, 255)

    # Fonts
    font_name = _pick_font(80 * SCALE)
    font_text = _pick_font(72 * SCALE)
    font_meta = _pick_font(52 * SCALE)


    # Base image
    img = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Avatar
    avatar = await _get_user_avatar(bot, author.id, AVATAR_SIZE)
    img.paste(avatar, (PADDING, PADDING), avatar)

    # Name + handle line
    display_name = author.full_name or (author.username and f"@{author.username}") or "Unknown"
    handle = f"@{author.username}" if author.username else ""
    text_x = PADDING + AVATAR_SIZE + 24
    text_y = PADDING

    draw.text((text_x, text_y), display_name, font=font_name, fill=NAME)
    if handle and handle != display_name:
        name_w = draw.textlength(display_name + "  ", font=font_name)
        draw.text((text_x + name_w, text_y + 10), handle, font=font_meta, fill=SUBTLE)

    # Quote bubble
    bubble_x = text_x
    bubble_y = text_y + 64
    bubble_w = W - PADDING - bubble_x
    bubble_h = H - bubble_y - PADDING

    # Rounded rectangle
    r = 24
    bubble = Image.new("RGBA", (bubble_w, bubble_h), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(bubble)
    bdraw.rounded_rectangle((0, 0, bubble_w, bubble_h), radius=r, fill=BUBBLE)
    img.paste(bubble, (bubble_x, bubble_y), bubble)

    # Quote text (wrapped)
    inner_pad = 28
    max_line_width = bubble_w - inner_pad * 2
    wrapped = _wrap_text(draw, text_to_quote, font_text, max_line_width)

    draw.multiline_text(
        (bubble_x + inner_pad, bubble_y + inner_pad),
        wrapped,
        font=font_text,
        fill=WHITE,
        spacing=6
    )

    # Send as photo
    bio = BytesIO()
    bio.name = "quote.png"
    img.save(bio, format="PNG")
    bio.seek(0)
    await bot.send_photo(chat_id=update.effective_chat.id, photo=bio)

# -------------------------------
# Register the handler
# -------------------------------
# from telegram.ext import ApplicationBuilder, CommandHandler
# app = ApplicationBuilder().token(BOT_TOKEN).build()
# app.add_handler(CommandHandler("quote", quote, block=False))  # works in groups/DMs
# app.run_polling()

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
def _load_font(size: int) -> ImageFont.FreeTypeFont:
    # Try DejaVu (bundled with Pillow). Fallback to default bitmap font.
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except Exception:
        return ImageFont.load_default()

def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    # Simple greedy word-wrap
    words = text.split()
    lines, cur = [], []
    for w in words:
        test = (" ".join(cur + [w])).strip()
        if draw.textlength(test, font=font) <= max_width:
            cur.append(w)
        else:
            if cur: lines.append(" ".join(cur))
            cur = [w]
    if cur: lines.append(" ".join(cur))
    return lines

def _make_round_avatar(img: Image.Image, size: int = 96) -> Image.Image:
    img = img.convert("RGB").resize((size, size))
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)
    return ImageOps.fit(img, (size, size), centering=(0.5, 0.5)).putalpha(mask) or Image.merge("RGBA", (*img.split(), mask))

async def _fetch_user_avatar_bytes(context, user_id: int) -> bytes | None:
    try:
        photos = await context.bot.get_user_profile_photos(user_id=user_id, limit=1)
        if photos.total_count and photos.photos:
            file_id = photos.photos[0][-1].file_id  # largest size
            file = await context.bot.get_file(file_id)
            bio = io.BytesIO()
            # PTB v21
            try:
                await file.download_to_memory(out=bio)
                return bio.getvalue()
            except Exception:
                # PTB v20 fallback
                data = await file.download_as_bytearray()
                return bytes(data)
    except Exception:
        pass
    return None

def _render_quote_card(pfp_img: Image.Image | None, display_name: str, handle: str | None, text: str) -> bytes:
    # Layout params
    W = 900
    P = 32
    AV = 96
    GAP = 20
    BUBBLE_PAD = 22
    NAME_SIZE = 36
    HANDLE_SIZE = 28
    TEXT_SIZE = 32
    BUBBLE_RADIUS = 22

    # Fonts
    font_name = _load_font(NAME_SIZE)
    font_handle = _load_font(HANDLE_SIZE)
    font_text = _load_font(TEXT_SIZE)

    # Create base (light background)
    base = Image.new("RGB", (W, 10), (248, 249, 250))
    draw = ImageDraw.Draw(base)

    # Prepare avatar (rounded)
    if pfp_img is not None:
        try:
            pfp_rgba = pfp_img.convert("RGBA")
            # Circular mask
            mask = Image.new("L", (AV, AV), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, AV, AV), fill=255)
            pfp_rgba = pfp_rgba.resize((AV, AV))
            avatar = Image.new("RGBA", (AV, AV))
            avatar.paste(pfp_rgba, (0, 0), mask)
        except Exception:
            avatar = None
    else:
        avatar = None

    # Measure header (name + handle)
    x = P + AV + GAP
    y = P
    name_w = draw.textlength(display_name, font=font_name)
    handle_w = draw.textlength(handle or "", font=font_handle)
    header_h = max(AV, int(font_name.size * 1.2) + (int(font_handle.size * 1.1) if handle else 0))

    # Wrap message text
    bubble_w = W - x - P
    text_lines = _wrap_text(draw, text, font_text, bubble_w - 2 * BUBBLE_PAD)
    line_heights = [font_text.getbbox(line)[3] - font_text.getbbox(line)[1] for line in text_lines] or [font_text.size]
    text_h = sum(line_heights) + (len(text_lines) - 1) * 6
    bubble_h = text_h + 2 * BUBBLE_PAD

    total_h = P + header_h + GAP + bubble_h + P
    base = base.resize((W, total_h))
    draw = ImageDraw.Draw(base)

    # Draw avatar
    if avatar:
        base.paste(avatar, (P, P), avatar)

    # Draw name & handle
    draw.text((x, y), display_name, font=font_name, fill=(20, 20, 20))
    if handle:
        draw.text((x, y + int(font_name.size * 1.2)), handle, font=font_handle, fill=(100, 100, 110))

    # Bubble rect
    bx1, by1 = x, P + header_h + GAP
    bx2, by2 = x + bubble_w, by1 + bubble_h
    # Rounded rectangle
    draw.rounded_rectangle([bx1, by1, bx2, by2], radius=BUBBLE_RADIUS, fill=(255, 255, 255), outline=(230, 232, 235), width=2)

    # Draw message text
    ty = by1 + BUBBLE_PAD
    for idx, line in enumerate(text_lines):
        draw.text((bx1 + BUBBLE_PAD, ty), line, font=font_text, fill=(25, 25, 26))
        ty += (font_text.getbbox(line)[3] - font_text.getbbox(line)[1]) + 6

    # Export bytes
    out = io.BytesIO()
    base.save(out, format="PNG")
    return out.getvalue()
from telegram import Update
from telegram.ext import ContextTypes
import asyncio

async def say(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    bot = context.bot
    chat_id = msg.chat_id

    try:
        # 1️⃣ If used as a reply: resend the replied message
        if msg.reply_to_message:
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=chat_id,
                message_id=msg.reply_to_message.message_id,
            )
        else:
            # 2️⃣ Otherwise, repeat the text after /say
            text = " ".join(context.args) if context.args else None
            if not text and msg.text:
                parts = msg.text.split(maxsplit=1)
                text = parts[1] if len(parts) > 1 else ""

            if text:
                if len(text) > 4096:
                    text = text[:4090] + "…"
                await bot.send_message(chat_id=chat_id, text=text)
            else:
                await msg.reply_text("Send `/say <text>` or reply to any message with `/say`.", parse_mode="Markdown")

    finally:
        # 3️⃣ Delete the user's command message (after a tiny delay)
        await asyncio.sleep(0.15)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
        except Exception:
            pass

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
# --- Admin gate helper (put after build_unmute_permissions) ---
async def _require_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    need_right: str | None = None,   # e.g. "can_delete_messages" or "can_restrict_members"
) -> bool:
    """Return True if caller is:
       - in OWNER_IDS (explicit allowed users), OR
       - the anonymous admin posting as the chat itself, OR
       - a chat admin/creator (and has need_right if specified).
    """
    msg = update.effective_message
    chat = update.effective_chat

    # 0) If sender is one of the owner IDs, allow immediately
    user = update.effective_user
    if user and getattr(user, "id", None) in globals().get("OWNER_IDS", set()):
        return True

    # 1) Anonymous admin mode: Telegram hides the user; allow if message is sent as the chat itself
    if getattr(msg, "sender_chat", None) and msg.sender_chat.id == chat.id:
        return True

    # 2) If no user (should be rare), deny
    if not user:
        return False

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        return False

    # 3) Must be an admin or creator
    if member.status not in ("administrator", "creator"):
        return False

    # 4) If a specific permission is required, creators bypass; admins must have it.
    if need_right and member.status != "creator":
        return bool(getattr(member, need_right, False))

    return True


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
        # Caller must be admin with "Restrict members"
    ok = await _require_admin(update, context, need_right="can_restrict_members")
    if not ok:
        await msg.reply_text("Only admins with the 'Restrict members' permission can use this.")
        return


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
    ok = await _require_admin(update, context, need_right="can_restrict_members")
    if not ok:
        await msg.reply_text("Only admins with the 'Restrict members' permission can use this.")
        return


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
        # Admin check: must be admin and have "Delete messages"
    ok = await _require_admin(update, context, need_right="can_delete_messages")
    if not ok:
        await msg.reply_text("Only admins with the 'Delete messages' permission can use this.")
        return

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

   
# ---------- /quote v5: Guaranteed TTF + really big text ----------
from io import BytesIO
from pathlib import Path
import os, re, hashlib
from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import Update
from telegram.ext import ContextTypes

# Try multiple places for a REAL TTF. If none found, we fallback to bundled Pillow path.
FONT_CANDIDATES = [
    # 1) project-local font (recommended: put DejaVuSans.ttf here)
    str(Path(__file__).parent / "fonts" / "DejaVuSans.ttf"),
    str(Path(__file__).parent / "fonts" / "Inter-Regular.ttf"),
    os.getenv("FONT_PATH") or "",

    # 2) common system locations
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",

    # 3) typical venv location on Railway/Docker
    "/app/.venv/lib/python3.11/site-packages/PIL/DejaVuSans.ttf",
]

from pathlib import Path
from PIL import ImageFont

def _q5_pick_font(size: int) -> ImageFont.FreeTypeFont:
    """Always load a real TTF from ./fonts/DejaVuSans.ttf. If missing, raise,
    so we never silently fall back to tiny bitmap fonts."""
    local = Path(__file__).parent / "fonts" / "DejaVuSans.ttf"
    if local.exists():
        return ImageFont.truetype(str(local), size=size)

    # Fallback: Pillow’s bundled DejaVu (second chance)
    try:
        pil_fonts = Path(ImageFont.__file__).parent / "fonts"
        dejavu = pil_fonts / "DejaVuSans.ttf"
        if dejavu.exists():
            return ImageFont.truetype(str(dejavu), size=size)
    except Exception:
        pass

    # If we got here, TTF isn’t available. Don’t proceed silently.
    raise RuntimeError("Missing TTF font. Place DejaVuSans.ttf in ./fonts/ (next to bot.py).")

def _q5_initials(name: str) -> str:
    parts = re.findall(r"\w+", name, flags=re.UNICODE)
    if not parts: return "?"
    if len(parts) == 1: return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()

def _q5_color_from_text(text: str) -> tuple[int,int,int]:
    h = hashlib.md5(text.encode("utf-8")).hexdigest()
    r = int(h[0:2], 16); g = int(h[2:4], 16); b = int(h[4:6], 16)
    return (100 + r % 120, 100 + g % 120, 100 + b % 120)

async def _q5_get_avatar_or_initials(bot, author, size: int) -> Image.Image:
    # Try real avatar
    try:
        photos = await bot.get_user_profile_photos(user_id=author.id, limit=1)
        if photos.total_count > 0:
            file = await bot.get_file(photos.photos[0][-1].file_id)
            b = await file.download_as_bytearray()
            img = Image.open(BytesIO(b)).convert("RGBA")
            img = ImageOps.fit(img, (size, size), method=Image.LANCZOS, centering=(0.5, 0.5))
            m = Image.new("L", (size, size), 0)
            ImageDraw.Draw(m).ellipse((0, 0, size, size), fill=255)
            img.putalpha(m)
            return img
    except Exception:
        pass
    # Fallback: initials circle
    circle = Image.new("RGBA", (size, size), (0,0,0,0))
    d = ImageDraw.Draw(circle)
    bg = _q5_color_from_text(author.full_name or author.username or "user")
    d.ellipse((0,0,size-1,size-1), fill=bg)
    f = _q5_pick_font(int(size*0.45))
    text = _q5_initials(author.full_name or author.username or "?")
    tw = d.textlength(text, font=f)
    th = f.getbbox(text)[3] - f.getbbox(text)[1]
    d.text(((size - tw)/2, (size - th)/2 - 2), text, font=f, fill=(255,255,255,255))
    return circle

def _q5_wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    words = text.split()
    if not words: return ""
    lines, cur = [], words[0]
    for w in words[1:]:
        if draw.textlength(cur + " " + w, font=font) <= max_width:
            cur += " " + w
        else:
            lines.append(cur); cur = w
    lines.append(cur)
    return "\n".join(lines)

async def quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    bot = context.bot
    target = msg.reply_to_message
    if target and (target.text or target.caption):
        text_to_quote = target.text or target.caption
        author = target.from_user
    else:
        text_to_quote = " ".join(context.args).strip()
        author = msg.from_user
    if not text_to_quote:
        await msg.reply_text("Reply to a message with /quote or use: /quote your text")
        return

            # IMPORTANT: we render at 1× and use HUGE font sizes, so even if bitmap font is used, it’s legible.
    W = 1600
    PAD = 56
    AV = 320

    BG = (18, 18, 18, 255)
    BUBBLE = (34, 34, 34, 255)
    NAME_C = (170, 152, 255, 255)
    TEXT_C = (245, 245, 245, 255)
    META_C = (200, 200, 200, 255)

    # MASSIVE sizes to force readability
    font_name = _q5_pick_font(520)   # username — extremely big
    font_text = _q5_pick_font(480)   # main quote — extremely big
    font_meta = _q5_pick_font(300)   # @handle — very big


        # Scratch canvas for measuring
    temp = Image.new("RGBA", (W, 10), BG)
    d0 = ImageDraw.Draw(temp)

    display_name = author.full_name or (author.username and f"@{author.username}") or "Unknown"
    handle = f"@{author.username}" if author.username else ""
    x_text = PAD + AV + 40
    y_top = PAD

    bubble_w = W - PAD - x_text
    inner_pad = 56
    wrapped = _q5_wrap(d0, text_to_quote, font_text, bubble_w - inner_pad * 2)
    text_bbox = d0.multiline_textbbox((0, 0), wrapped, font=font_text, spacing=18)
    text_h = text_bbox[3] - text_bbox[1]
    bubble_h = text_h + inner_pad * 2

    # Height of the name + (optional) handle ONLY
    name_bbox = d0.textbbox((0, 0), display_name, font=font_name)
    name_h = name_bbox[3] - name_bbox[1]
    handle_h = 0
    if handle:
        hb = d0.textbbox((0, 0), handle, font=font_meta)
        handle_h = hb[3] - hb[1]

    GAP_NAME = 20  # gap between handle and bubble
    header_h_name = name_h + (handle_h if handle else 0)

    # Bubble begins right under name/handle (even if avatar is taller)
    by = y_top + header_h_name + GAP_NAME

    # Canvas height must fit both the bubble and the avatar
    H = max(by + bubble_h + PAD, PAD + AV + PAD)

    # === NOW create the base image ===
    img = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Avatar
    avatar = await _q5_get_avatar_or_initials(bot, author, AV)
    img.paste(avatar, (PAD, y_top), avatar)

    # Name + handle
    draw.text((x_text, y_top), display_name, font=font_name, fill=NAME_C)
    if handle and handle != display_name:
        nm_w = draw.textlength(display_name + "  ", font=font_name)
        draw.text((x_text + nm_w, y_top + 12), handle, font=font_meta, fill=META_C)

    # Quote bubble
    r = 42
    bubble = Image.new("RGBA", (bubble_w, bubble_h), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(bubble)
    bdraw.rounded_rectangle((0, 0, bubble_w, bubble_h), radius=r, fill=BUBBLE)
    img.paste(bubble, (x_text, by), bubble)

    # Quote text
    draw.multiline_text(
        (x_text + inner_pad, by + inner_pad),
        wrapped,
        font=font_text,
        fill=TEXT_C,
        spacing=18,
    )

    bio = BytesIO(); bio.name = "quote.png"
    img.save(bio, format="PNG"); bio.seek(0)
    await bot.send_photo(chat_id=update.effective_chat.id, photo=bio)
# ---------- end /quote v5 ----------





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

    # --- Command handlers ---
    app.add_handler(
        CommandHandler(
            ["quote", "q"],
            quote,
            filters=filters.ChatType.PRIVATE | filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP,
        )
    )
    app.add_handler(CommandHandler("yemek", yemek))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CommandHandler(["say", "echo"], say))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"^-sms\s+\d{1,3}$"),
            sms_purge,
        )
    )

    # ✅ only 4 spaces here (inside def main)
    print("🤖 Bot is running... Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()

