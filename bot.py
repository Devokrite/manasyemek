import asyncio
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from urllib.parse import urljoin
import aiohttp
from bs4 import BeautifulSoup

import hashlib
import hmac
import secrets
import json
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
# ====== SCHEDULE (edit these as you like) ======
# Keys must be Python weekday numbers: Monday=0 ... Sunday=6
SCHEDULE: dict[int, list[str]] = {
    0: [  # Понедельник
        "08:55–09:40 КЫРГЫЗСКИЙ ЯЗЫК И ЛИТЕРАТУРА I — Бакытбек Джунусалиев (ИИБФ 317)",
        "09:50–10:35 КЫРГЫЗСКИЙ ЯЗЫК И ЛИТЕРАТУРА I — Бакытбек Джунусалиев (ИИБФ 317)",
        "11:40–12:25 ОБЩАЯ БУХГАЛТЕРИЯ I  — Уланбек Молдокматов (ИИБФ 326)",
        "13:30–14:15 ОБЩАЯ БУХГАЛТЕРИЯ I  — Уланбек Молдокматов (ИИБФ 326)",
        "14:25–15:10 ОБЩАЯ БУХГАЛТЕРИЯ I  — Уланбек Молдокматов (ИИБФ 326)",
    ],
    1: [  # Вторник
        "08:55–09:40 МАТЕМАТИКА I — Мирбек Токтосунов (ИИБФ 324)",
        "09:50–10:35 МАТЕМАТИКА I — Мирбек Токтосунов (ИИБФ 324)",
        "11:40–12:25 ФИЗИЧЕСКАЯ КУЛЬТУРА I — Салтанат Кайкы (КССБ спортзал №01)",
        "13:30–14:15 ФИЗИЧЕСКАЯ КУЛЬТУРА I — Салтанат Кайкы (КССБ спортзал №01)",
        "14:25–15:10 ФИЗИЧЕСКАЯ КУЛЬТУРА I — Салтанат Кайкы (КССБ спортзал №01)",
    ],
    2: [  # Среда
        "10:45–11:30 МАТЕМАТИКА I — Мирбек Токтосунов (ИИБФ 324)",
        "11:40–12:25 МАТЕМАТИКА I — Мирбек Токтосунов (ИИБФ 324)",
        "13:30–14:15 ВВЕДЕНИЕ В МЕНЕДЖМЕНТ  — Азамат Максудунов (ИИБФ 323)",
        "14:25–15:10 ВВЕДЕНИЕ В МЕНЕДЖМЕНТ  — Азамат Максудунов (ИИБФ 323)",
    ],
    3: [],  # Четверг — нет занятий
    4: [  # Пятница
        "08:55–09:40 ВВЕДЕНИЕ В ПРАВО — Медербек Оролбаев (ИИБФ 521)",
        "09:50–10:35 ВВЕДЕНИЕ В ПРАВО — Медербек Оролбаев (ИИБФ 321)",
        "10:45–11:30 ВВЕДЕНИЕ В ПРАВО — Медербек Оролбаев (ИИБФ 321)",
        "12:35–13:20 ВВЕДЕНИЕ В ЭКОНОМИКУ I — Джунус Ганиев (ИИБФ А-205)",
        "14:25–15:10 ВВЕДЕНИЕ В ЭКОНОМИКУ I — Джунус Ганиев (ИИБФ А-205)",
        "16:15–17:00 ВВЕДЕНИЕ В ЭКОНОМИКУ I — Джунус Ганиев (ИИБФ А-205)",
    ],
    5: ["Отдых"],  # Суббота
    6: ["Отдых"],  # Воскресенье
}

# ====== Улучшенный формат расписания ======
_item_re = re.compile(
    r"^(?P<time>\d{2}:\d{2}–\d{2}:\d{2})\s+"
    r"(?P<subject>.+?)\s+—\s+"
    r"(?P<teacher>.+?)\s+\((?P<room>.+)\)$"
)

def _pretty_item(raw: str) -> str:
    m = _item_re.match(raw)
    if not m:
        return f"• {raw}"
    t = m.groupdict()
    return (
        f"• <b>{t['subject']}</b>  <code>{t['time']}</code>\n"
        f"  <i>{t['teacher']}</i> · {t['room']}"
    )

DAY_NAMES_RU = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]

def _fmt_day_lines(dt: datetime) -> str:
    wd = dt.weekday()
    title = f"📅 <b>{DAY_NAMES_RU[wd]} ({dt.strftime('%d.%m')})</b>"
    items = SCHEDULE.get(wd, [])
    if not items:
        return f"{title}\nЗанятий нет 🙂"
    body = "\n\n".join(_pretty_item(x) for x in items)
    return f"{title}\n\n{body}"

def _week_bounds(dt: datetime) -> tuple[datetime, datetime]:
    monday = dt - timedelta(days=dt.weekday())
    return monday, monday + timedelta(days=6)

def _fmt_week(dt: datetime) -> str:
    monday, _ = _week_bounds(dt)
    parts = []
    cur = monday
    for _ in range(7):
        parts.append(_fmt_day_lines(cur))
        cur += timedelta(days=1)
    return "\n\n──────────\n\n".join(parts)


# ===== Crocodile Game CONFIG =====
CROC_WORDS = [
    "Айдан","Бакай","Саид","Айдар","Жайдар","Салат","Саламат",
    "Саадат","Адинай","Кайрат","Кокос","Матиза","Матиз",
    "Дымка","Краат","Укук","Фараон","Акжол","Турец",
    "Манас","Бункер","Холера","Чума","Мороженщица","склероз",
    "Мафия","Джал","Менеджмент","Сундук","Пельмени","Дичь",
    "Айтматов","Мирбек","Нурадиль","Кант","Ишлетме","дракон",
    "Энцефалит","Булимия","Наруто","Чатжпт","Чурка","Инженер",
    "Айдана","Трешка","ДебеТ","Азамат","Азим","Убубвэвэосас",
    "Мухасебе","67","аура","Асель","Уно","Акчуч",
    "Садыр","Ташиев","Нудизм","эксгибиционизм","Дон","Путана",
    "Йемек","Бруни","Пидэ","Фара","Ширинка","Жайдар",
    "Матрица","Кейкап","Сосисли","Тост","Карышык","Кырпык",
    "Бухучет","Лабораторная физика","Акжоленок","Фараончик","Айдай","Йоклама",
    "Кпоп","Таккаунт","Мээрим","Кайратик","Омск","Верю не верю",
    "Сушеные бананы","Шалун","Шалунишка","Шлюшка","Бисмиллях","Карапуз",
    "Книга братан","Азиямолл","Алаарча","Квест","Утипути","окр", "Алибургер",
    "Галушка","Молоко","Резак","Четверг","Макаронсы","Мохнатость","Демирбанк","Инженеры",
    "Мужчина","Боксер","Айпери","Фит","Мазда","Пальма","Мохнатость","Окр","Айслатте на миндальном","Априорность","Апостериорности",
    "Ношпа","Миллениал", "Шредер","Мегамозг","Сигмабой","Сигмагерл","Попыт","Симплдимпл","Егор крид","Эдвард","Бродяга","Джейкоб","Оборотень","Кравосися","Гольф",


    
]
CROC_SCORES_FILE = Path("croc_scores.json")


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("manas_menu_bot")




# ====== Croc state ======
CROC_GAMES: dict[int, dict] = {}           # chat_id -> {explainer_id, explainer_name, word, used:set[str]}
CROC_LOCKS: dict[int, asyncio.Lock] = {}   # per-chat lock
CROC_SCORES: dict[str, dict[str, dict]] = {}  # {"chat_id": {"user_id": {"name": str, "points": float}}}

def _croc_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in CROC_LOCKS:
        CROC_LOCKS[chat_id] = asyncio.Lock()
    return CROC_LOCKS[chat_id]

def _croc_pick_word(chat_id: int) -> str:
    used = CROC_GAMES.get(chat_id, {}).get("used", set())
    pool = [w for w in CROC_WORDS if w not in used] or CROC_WORDS[:]
    return random.choice(pool)

def _croc_norm(s: str) -> str:
    # Lowercase, swap ё->е, remove most punctuation/emoji, collapse spaces
    s = (s or "").lower().replace("ё", "е")
    # Keep letters/digits/spaces only
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)
    # Collapse whitespace
    s = " ".join(s.split())
    return s


def _croc_add_points(chat_id: int, user_id: int, name: str, pts: float):
    c = str(chat_id); u = str(user_id)
    CROC_SCORES.setdefault(c, {})
    CROC_SCORES[c].setdefault(u, {"name": name, "points": 0.0})
    CROC_SCORES[c][u]["name"] = name
    CROC_SCORES[c][u]["points"] = float(CROC_SCORES[c][u]["points"]) + float(pts)
    try:
        with open(CROC_SCORES_FILE, "w", encoding="utf-8") as f:
            json.dump(CROC_SCORES, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"[CROC] save scores failed: {e}")

def _croc_load_scores():
    global CROC_SCORES
    if CROC_SCORES_FILE.exists():
        try:
            CROC_SCORES = json.load(open(CROC_SCORES_FILE, "r", encoding="utf-8"))
        except Exception:
            CROC_SCORES = {}

def _croc_board(chat_id: int) -> str:
    c = str(chat_id)
    if not CROC_SCORES.get(c):
        return "Пока нет очков. Запустите раунд: /croc ✨"
    arr = [(v["points"], v["name"]) for v in CROC_SCORES[c].values()]
    arr.sort(key=lambda x: x[0], reverse=True)
    lines = ["🏆 *Рейтинг чата:*"]
    for i, (pts, name) in enumerate(arr[:15], start=1):
        lines.append(f"{i}. {name} — *{pts:.1f}*")
    return "\n".join(lines)

# ===== PREDICTIONS CONFIG (hard-coded) =====
# Map Telegram user_id -> real name (in Russian). Fill your people here:
REAL_NAMES: dict[int, str] = {
    # Примеры:
     738347292: "Байтур",
     1119666458: "Жайдарски",
     984162618: "🎀Матизочка🎀",
     838410534: "Caид",
     873829541: "Салтанат",
     1165162268: "Бакай",
     1064290505: "Кокос",
     7687350164: "Мээрим",
     987503187: "Айганыш",
     862779556: "Айдана 🕷️",
    
}

# Русские предсказания. Можно дополнять/менять — одна строка = одно предсказание.
PREDICTIONS_RU: list[str] = [
    "Сегодня у тебя получится то, что долго откладывал(а).",
    "67 - Это твой минимум на финале.",
    "Ты не опоздаешь. Всё просто начнётся без тебя.",
    "Твоя лень — это просто организм, который копит энергию на великие дела (возможно)",
    "Небольшое рискованное решение принесёт хороший результат.",
    "Неожиданное сообщение улучшит настроение.",
    "Сфокусируйся на одном деле — получишь больше, чем ожидаешь.",
    "Короткая прогулка наведёт порядок в мыслях.",
    "Комплимент, который ты сделаешь, вернётся к тебе вдвойне.",
    "Откровенный разговор снимет лишнее напряжение.",
    "Одна смелая мысль приведёт к маленькому прорыву.",
    "Не бойся попросить помощь — это ускорит результат.",
    "Найденное слово окажется ключом к решению.",
    "Сегодня твоя энергия заразительна — делись ею.",
    "Новая идея придёт в самый неожиданный момент.",
    "Чем меньше думаешь — тем быстрее всё получится.",
    "Ты вдохновишь кого-то своим примером.",
    "Настало время отпустить старое и впустить новое.",
    "Улыбка сегодня откроет больше дверей, чем логика.",
    "Твоё терпение сегодня — главный козырь.",
    "Кто-то вспоминает тебя с благодарностью.",
    "Твоя интуиция сегодня необычайно точна.",
    "Путь, который казался сложным, окажется лёгким.",
    "Ты удивишься, насколько просто всё решается.",
    "Случайная встреча окажется неслучайной.",
    "День принесёт повод гордиться собой.",
    "День начнётся с хаоса, но закончится ясностью.",
    "Лучше сделать один шаг, чем сто раз подумать.",
    "Сегодня стоит слушать, а не говорить.",
    "Не спорь с теми, кто не слышит — просто сделай по-своему.",
    "Твоя уверенность заразит других.",
    "Что-то из прошлого неожиданно вернётся с добром.",
    "День подходит для искренних разговоров.",
    "Отдых сегодня принесёт больше пользы, чем усилия.",
    "Твоя доброта вернётся быстро.",
    "Сегодня всё будет складываться лучше, чем ты думаешь.",
    "Важное решение придёт во сне — доверься ему.",
    "Хаос сегодня временный — не теряй самообладания.",
    "Будь мягче — и мир станет добрее к тебе.",
    "Случайное слово кого-то заденет — будь внимателен.",
    "Тебе стоит прислушаться к первой мысли.",
    "То, что ты считаешь ошибкой, обернётся подарком.",
    "Смелость сегодня вознаграждается.",
    "Лучше начать, чем ждать идеального момента.",
    "Ты найдёшь вдохновение в мелочах.",
    "Ожидание затянется, но результат того стоит.",
    "Сегодня кто-то увидит в тебе поддержку.",
    "Твоё спокойствие заразительно.",
    "Новый взгляд решит старую проблему.",
    "Случайность сегодня — лучшее из планов.",
    "Порадуй себя чем-то маленьким.",
    "Ты на правильном пути — даже если сомневаешься.",
    "Твоя энергия способна многое изменить сегодня.",
    "Будь честен с собой — это начнёт цепочку удач.",
    "Смех сегодня — лекарство от всего.",
    "Ты сможешь больше, чем кажется.",
    "Настало время сказать «нет» чему-то лишнему.",
    "Сегодня ты притягиваешь удачу.",
    "Кто-то тайно восхищается тобой.",
    "Пора перестать ждать разрешения и просто сделать.",
    "Сегодня ты удивишь даже себя.",
    "Будь готов к хорошим новостям.",
    "Отпусти контроль — и всё само выстроится.",
    "Вселенная готовит тебе маленький подарок.",
]
# Чтобы не повторять одну и ту же строчку подряд для одного пользователя
from collections import defaultdict, deque
_LAST_PICKS: dict[int, deque[int]] = defaultdict(lambda: deque(maxlen=2))


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
        from telegram.ext import CallbackQueryHandler  # you already have; just ensure it’s imported

CROC_CB_PREFIX = "croc:"  # callback data prefix

async def _croc_start_round(context: ContextTypes.DEFAULT_TYPE, chat_id: int, explainer_user):
    explainer_id = explainer_user.id
    explainer_name = (
        explainer_user.full_name
        or (explainer_user.username and f"@{explainer_user.username}")
        or f"id:{explainer_id}"
    )

    used_prev = set()
    if chat_id in CROC_GAMES and isinstance(CROC_GAMES[chat_id].get("used"), set):
        used_prev = CROC_GAMES[chat_id]["used"]

    word = _croc_pick_word(chat_id)
    used_prev.add(word)

    CROC_GAMES[chat_id] = {
        "explainer_id": explainer_id,
        "explainer_name": explainer_name,
        "word": word,
        "used": used_prev,
    }

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔐 Показать слово", callback_data=f"{CROC_CB_PREFIX}show:{chat_id}:{explainer_id}"),
        InlineKeyboardButton("⏭ Пропустить", callback_data=f"{CROC_CB_PREFIX}skip:{chat_id}:{explainer_id}"),
        InlineKeyboardButton("🛑 Завершить", callback_data=f"{CROC_CB_PREFIX}end:{chat_id}:{explainer_id}"),
    ]])

    # ONE message only:
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🎬 Раунд начался! Объясняет: *{explainer_name}*\n"
            f"Нажми «Показать слово», чтобы увидеть слово."
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )

async def croc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await msg.reply_text("Команду /croc нужно вызывать в группе.")
        return

    lock = _croc_lock(chat.id)
    async with lock:
        if chat.id in CROC_GAMES:
            g = CROC_GAMES[chat.id]
            await msg.reply_text(
                f"Уже идёт раунд. Объясняет: {g['explainer_name']}.\n"
                f"Нажмите *Показать слово* под сообщением.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Start the round ONCE; the helper sends the only message.
        await _croc_start_round(context, chat.id, user)


async def croc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer(cache_time=0)  # we’ll re-answer with alert below if needed

    try:
        action, chat_id_s, explainer_id_s = q.data.split(":")[1:]
        chat_id = int(chat_id_s)
        explainer_id = int(explainer_id_s)
    except Exception:
        return

    # Only the explainer may press these buttons
    if not q.from_user or q.from_user.id != explainer_id:
        await q.answer("Только объясняющий может пользоваться этими кнопками.", show_alert=True)
        return

    lock = _croc_lock(chat_id)
    async with lock:
        g = CROC_GAMES.get(chat_id)
        if not g or g.get("explainer_id") != explainer_id:
            await q.answer("Раунд уже не активен.", show_alert=True)
            return

        if action == "show":
            await q.answer(text=f"ТВОЁ СЛОВО:\n\n{g['word']}", show_alert=True)
            return

        if action == "skip":
            new_word = _croc_pick_word(chat_id)
            g["word"] = new_word
            g["used"].add(new_word)
            await q.answer(text=f"НОВОЕ СЛОВО:\n\n{new_word}", show_alert=True)
            try:
                await q.edit_message_reply_markup(reply_markup=q.message.reply_markup)
            except Exception:
                pass
            return

        if action == "end":
            CROC_GAMES.pop(chat_id, None)
            await q.answer("Раунд завершён.", show_alert=True)
            try:
                await q.message.reply_text("🛑 Раунд завершён организатором.")
            except Exception:
                pass
            return

async def croc_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text("Рейтинг доступен только в группе.")
        return
    await update.effective_message.reply_text(_croc_board(chat.id), parse_mode=ParseMode.MARKDOWN)
def _levenshtein_leq1(a: str, b: str) -> bool:
    """True if Levenshtein distance <= 1 (one insert/delete/substitute)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    # ensure a is the shorter
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    i = j = diff = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1; j += 1
        else:
            diff += 1
            if diff > 1:
                return False
            if la == lb:
                i += 1; j += 1    # substitute
            else:
                j += 1            # insert/delete in longer string
    if j < lb or i < la:
        diff += 1
    return diff <= 1


async def croc_group_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not msg or not msg.text:
        return

    original = msg.text or ""
    text_norm = _croc_norm(original)

    g = CROC_GAMES.get(chat.id)
    if not g:
        return

    target = _croc_norm(g["word"])
    words = re.findall(r"\w+", text_norm, flags=re.UNICODE)

    # Explainer cannot say the word
    if user.id == g["explainer_id"]:
        if text_norm == target or target in words:
            try:
                await msg.reply_text("⚠️ Нельзя произносить слово напрямую — объясняй иначе!")
            except Exception:
                pass
        return

    # ----- Guess evaluation -----
    # exact (ё==е) if whole message equals OR token equals
    is_exact = (text_norm == target) or (target in words)

    if is_exact:
        guesser_name = user.full_name or (user.username and f"@{user.username}") or f"id:{user.id}"
        _croc_add_points(chat.id, user.id, guesser_name, 1.0)
        _croc_add_points(chat.id, g["explainer_id"], g["explainer_name"], 0.5)
        try:
            await msg.reply_text(
                f"🎉 Правильно! {guesser_name} угадал слово — *{g['word']}*.\n"
                f"+1.0 {guesser_name}, +0.5 {g['explainer_name']}.\n"
                f"▶️ Следующий раунд: объясняет {guesser_name}.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

        # Start next round WITH THE GUESSER (protect with lock)
        lock = _croc_lock(chat.id)
        async with lock:
            await _croc_start_round(context, chat.id, user)
        return

    # Close (but not correct): one-letter typo (len>=4) -> hint only, no points
    if len(target) >= 4:
        if _levenshtein_leq1(text_norm, target) or any(_levenshtein_leq1(w, target) for w in words):
            try:
                await msg.reply_text("🔎 Почти! Ты очень близко — проверь одну букву.")
            except Exception:
                pass
    # otherwise ignore



    target_raw = g["word"]
    target = _croc_norm(target_raw)

    # If explainer says the word -> warn & ignore
    if user.id == g["explainer_id"]:
        # exact after normalization OR standalone word check
        if text == target or re.search(rf"(?<!\w){re.escape(target)}(?!\w)", original.lower().replace("ё","е")):
            try:
                await msg.reply_text("⚠️ Нельзя произносить слово напрямую — объясняй иначе!")
            except Exception:
                pass
        return

    # ACCEPT if:
    # 1) whole message equals normalized target
    # 2) target appears as a standalone word anywhere in original text (ё->е normalized)
    # 3) message has a single-typo variant of target (for words >= 4)
    ok = (
        text == target
        or re.search(rf"(?<!\w){re.escape(target)}(?!\w)", original.lower().replace("ё","е")) is not None
        or (len(target) >= 4 and _levenshtein_leq1(text, target))
    )
    if not ok:
        return

    guesser_name = user.full_name or (user.username and f"@{user.username}") or f"id:{user.id}"
    _croc_add_points(chat.id, user.id, guesser_name, 1.0)
    _croc_add_points(chat.id, g["explainer_id"], g["explainer_name"], 0.5)

    try:
        await msg.reply_text(
            f"🎉 Правильно! {guesser_name} угадал слово — *{g['word']}*.\n"
            f"+1.0 {guesser_name}, +0.5 {g['explainer_name']}.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass

    CROC_GAMES.pop(chat.id, None)


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

    # === Save as WEBP sticker ===
    bio = BytesIO()
    bio.name = "quote.webp"
    # Resize safely to Telegram sticker size limit (max 512x512)
    max_side = 512
    w, h = img.size
    scale = min(max_side / w, max_side / h, 1)
    if scale < 1:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    img.save(bio, format="WEBP", lossless=True)
    bio.seek(0)

    # Send as sticker instead of photo
    await bot.send_sticker(chat_id=update.effective_chat.id, sticker=bio)

# -------------------------------
# Register the handler
# -------------------------------
# from telegram.ext import ApplicationBuilder, CommandHandler
# app = ApplicationBuilder().token(BOT_TOKEN).build()
# app.add_handler(CommandHandler("quote", quote, block=False))  # works in groups/DMs
# app.run_polling()

DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}\s+\S+", re.U)

# =======================
# OPTIMIZED HELPERS
# =======================

async def fetch_menu_html_async() -> str:
    # Check cache first
    if time.time() - _cache["ts"] < CACHE_TTL and _cache["raw"]:
        return _cache["raw"]
    
    # Use aiohttp for non-blocking download
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MenuBot/3.1)",
        "Accept-Language": "tr-TR,tr;q=0.9,ru;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(MENU_URL, headers=headers, timeout=15) as resp:
                resp.raise_for_status()
                text = await resp.text()
                # Update Cache
                _cache["raw"] = text
                _cache["parsed"] = None
                _cache["ts"] = time.time()
                return text
        except Exception as e:
            log.error(f"Network error: {e}")
            raise

async def tr_async(text: str) -> str:
    """Runs the blocking translator in a separate thread."""
    if not text: 
        return ""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, 
            lambda: GoogleTranslator(source="auto", target="ru").translate(text)
        )
    except Exception:
        return text

async def parse_menu_async(html: str):
    if _cache["parsed"] is not None and _cache["raw"] == html:
        return _cache["parsed"]

    soup = BeautifulSoup(html, "html.parser")
    result = OrderedDict()

    # 1. First pass: Collect all items
    all_items_flat = [] 
    
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

        day_items = []
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
                item_obj = {
                    "name": name,
                    "name_ru": name, 
                    "kcal": kcal,
                    "img": img_url,
                }
                day_items.append(item_obj)
                all_items_flat.append(item_obj)

        if day_items:
            result[date_text] = day_items

    # 2. Second pass: Translate EVERYTHING in parallel
    tasks = [tr_async(item["name"]) for item in all_items_flat]
    
    if tasks:
        translations = await asyncio.gather(*tasks)
        for item, ru_text in zip(all_items_flat, translations):
            item["name_ru"] = ru_text

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

# ===================== /schedule =====================
# ---- Departments & Days (STATIC MODE) ----
DEPARTMENTS = [
    ("management", "Management"),
    ("programming", "Programming"),
    ("electrical", "Electrical Engineering"),
    ("biology", "Biology"),
]
DAYS = [
    ("today", "Сегодня"),
    ("tomorrow", "Завтра"),
    ("week", "Вся неделя"),
]
DEPT_LABEL = {k: v for k, v in DEPARTMENTS}

def kb_departments() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text=label, callback_data=f"sch:dept:{key}")]
         for key, label in DEPARTMENTS]
    )

def kb_days(dept_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text=label, callback_data=f"sch:day:{dept_key}:{day_key}")]
         for day_key, label in DAYS]
    )

# --- DROP YOUR LESSONS HERE ---
# Format idea: each day is a list of lines; "week" will just join them by day.
# ---- COMPUTER ENGINEERING (Bilgisayar Mühendisliği) ----
SCHED_PROGRAMMING = {
    "monday": [
        "08:00–08:45 | Reserved (Ayrılmış)",
        "08:55–09:40 | UME-1101 Fizik I (Uygulama) | Nazgul Abdanbayeva | MFFB 118",
        "09:50–10:35 | BIL-1012 Bilgisayar Mühendisliğine Giriş | Cinara Cumabayeva | MFFB 524",
        "10:45–11:30 | UME-1101 Fizik I (Uygulama) | Nazgul Abdanbayeva | MFFB 118",
        "11:40–12:25 | BIL-1010 Bilgisayar Mühendisliğine Giriş | Mehmet Kenan Dönmez | MFFB 524",
        "13:30–14:15 | MAT-173 Cebir ve Uygulamaları | Peyil Tesengul Kızı | IIBF 128",
        "14:25–15:10 | MAT-173 Cebir ve Uygulamaları | Peyil Tesengul Kızı | IIBF 128",
        "16:15–17:00 | BES-111(FK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
        "17:10–17:55 | BES-111(FK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
    ],
    "tuesday": [
        "08:00–08:45 | UME-1101 Fizik I (Uygulama) | Nazgul Abdanbayeva | MFFB 118",
        "08:55–09:40 | MAT-17105 Matematik I | Asan Bayzakov | MFFB 301",
        "09:50–10:35 | MAT-17105 Matematik I | Asan Bayzakov | MFFB 301",
        "10:45–11:30 | MAT-17105 Matematik I | Asan Bayzakov | MFFB 301",
        "11:40–12:25 | MAT-17105 Matematik I | Asan Bayzakov | MFFB 301",
        "13:30–14:15 | BIL-1017 Programlama Dilleri I (Yan ve Ödev) | Bakıt Şarşembayev | MFFB 525",
        "14:25–15:10 | BIL-1017 Programlama Dilleri I (Yan ve Ödev) | Bakıt Şarşembayev | MFFB 525",
        "15:20–16:05 | BIL-1017 Programlama Dilleri I (Yan ve Ödev) | Bakıt Şarşembayev | MFFB 525",
        "16:15–17:00 | BES-111(FK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
        "17:10–17:55 | BES-111(FK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
    ],
    "wednesday": [
        "08:00–08:45 | Reserved (Ayrılmış)",
        "08:55–09:40 | UME-1102 Genel Fizik I | Tamara Karaşeva | IIBF 128",
        "09:50–10:35 | UME-1102 Genel Fizik I | Tamara Karaşeva | IIBF 128",
        "10:45–11:30 | UME-1102 Genel Fizik I | Tamara Karaşeva | IIBF 128",
        "11:40–12:25 | UME-1102 Genel Fizik I | Tamara Karaşeva | IIBF 128",
        "13:30–14:15 | BIL-1017 Programlama Dilleri I (Yan ve Ödev) | Bakıt Şarşembayev | MFFB 525",
        "14:25–15:10 | BIL-1017 Programlama Dilleri I (Yan ve Ödev) | Bakıt Şarşembayev | MFFB 525",
        "15:20–16:05 | BIL-1017 Programlama Dilleri I (Yan ve Ödev) | Bakıt Şarşembayev | MFFB 525",
        "16:15–17:00 | BES-111(FK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
        "17:10–17:55 | BES-111(FK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
    ],
    "thursday": [
        "08:55–09:40 | MAT-17105 Matematik I | Asan Bayzakov | IIBF 127",
        "09:50–10:35 | MAT-17105 Matematik I | Asan Bayzakov | IIBF 127",
        "10:45–11:30 | MAT-17105 Matematik I | Asan Bayzakov | IIBF 127",
        "11:40–12:25 | MAT-17105 Matematik I | Asan Bayzakov | IIBF 127",
        "13:30–14:15 | KGZ-103 Kırgız Dili ve Edebiyatı I | Aynura Beyşeyeva | IIBF 127",
        "14:25–15:10 | KGZ-103 Kırgız Dili ve Edebiyatı I | Aynura Beyşeyeva | IIBF 127",
        "15:20–16:05 | BES-111 Beden Eğitimi ve Spor I | Sıyimyk Artasınbekov | KSSB Spor Sahası",
        "16:15–17:00 | ING-111 İngilizce I | Svetlana Çenebekova | MFFB Online",
        "17:10–17:55 | ING-111 İngilizce I | Svetlana Çenebekova | MFFB Online",
    ],
    "friday": [
        "08:55–09:40 | UME-1101 Genel Fizik I | Tamara Karaşeva | IIBF 128",
        "09:50–10:35 | UME-1101 Genel Fizik I | Tamara Karaşeva | IIBF 128",
        "10:45–11:30 | UME-1101 Genel Fizik I | Tamara Karaşeva | IIBF 128",
        "11:40–12:25 | UME-1101 Genel Fizik I | Tamara Karaşeva | IIBF 128",
        "13:30–14:15 | BES-111 Beden Eğitimi ve Spor I (Teorik) | Atila Çakar | KSSB Online Sport",
        "14:25–15:10 | BES-111 Beden Eğitimi ve Spor I (Teorik) | Atila Çakar | KSSB Online Sport",
    ],
    "saturday": [],
    "sunday": [],
}


# ---- ELECTRICAL & ELECTRONICS ENGINEERING (Elektrik-Elektronik Mühendisliği) ----
SCHED_ELECTRICAL = {
    "monday": [
        "08:55–09:40 | EEM-143 C Programlama | Aybek Adanbayev | IIBF 111",
        "09:50–10:35 | EEM-143 C Programlama | Aybek Adanbayev | IIBF 111",
        "10:45–11:30 | EEM-143 C Programlama | Aybek Adanbayev | IIBF 111",
        "11:40–12:25 | EEM-143 C Programlama | Aybek Adanbayev | IIBF 111",
        "13:30–14:15 | UME-1105 Fizik I (Uygulama) | Azat Akmatbekova | MFFB 118",
        "14:25–15:10 | UME-1105 Fizik I (Uygulama) | Azat Akmatbekova | MFFB 118",
        "16:15–17:00 | BES-111(LFK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
        "17:10–17:55 | BES-111(LFK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
    ],
    "tuesday": [
        "08:55–09:40 | MAT-17105 Matematik I | Asan Bayzakov | MFFB 301",
        "09:50–10:35 | MAT-17105 Matematik I | Asan Bayzakov | MFFB 301",
        "10:45–11:30 | KMM-109.02 Genel Kimya (Teori) | Ferhan Tümer | MFFB 401",
        "11:40–12:25 | KMM-109.02 Genel Kimya (Teori) | Ferhan Tümer | MFFB 401",
        "13:30–14:15 | EIM-113.01 Mesleki Yabancı Dil I (İngilizce) | Köksal Erentürk | MFFB 513",
        "14:25–15:10 | EIM-113.01 Mesleki Yabancı Dil I (İngilizce) | Köksal Erentürk | MFFB 513",
        "16:15–17:00 | BES-111(LFK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
        "17:10–17:55 | BES-111(LFK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
    ],
    "wednesday": [
        "08:55–09:40 | UME-111 Genel Fizik I | Meerim İmaş Kızı | MFFB 303",
        "09:50–10:35 | UME-111 Genel Fizik I | Meerim İmaş Kızı | MFFB 303",
        "10:45–11:30 | UME-111 Genel Fizik I | Meerim İmaş Kızı | MFFB 303",
        "11:40–12:25 | UME-111 Genel Fizik I | Meerim İmaş Kızı | MFFB 303",
        "13:30–14:15 | KMM-109.08 Genel Kimya (Uygulama) | Nurzat Şaykieva | MFFB 321",
        "14:25–15:10 | KMM-109.08 Genel Kimya (Uygulama) | Nurzat Şaykieva | MFFB 321",
    ],
    "thursday": [
        "08:55–09:40 | KGZ-103 Kırgız Dili ve Edebiyatı I | Aynura Beyşeyeva | MFFB 106",
        "09:50–10:35 | KGZ-103 Kırgız Dili ve Edebiyatı I | Aynura Beyşeyeva | MFFB 106",
        "10:45–11:30 | KGZ-103 Kırgız Dili ve Edebiyatı I | Aynura Beyşeyeva | MFFB 106",
        "11:40–12:25 | KGZ-103 Kırgız Dili ve Edebiyatı I | Aynura Beyşeyeva | MFFB 106",
        "13:30–14:15 | BES-111 Beden Eğitimi ve Spor I | Sıyimyk Artasınbekov | KSSB Spor Sahası (Kapalı)",
        "14:25–15:10 | BES-111 Beden Eğitimi ve Spor I | Sıyimyk Artasınbekov | KSSB Spor Sahası (Kapalı)",
    ],
    "friday": [
        "08:55–09:40 | EEM-103 Elektrik-Elektronik Mühendisliğine Giriş ve Kariyer Planlama | Mehmet Karadeniz | MFFB 501",
        "09:50–10:35 | EEM-103 Elektrik-Elektronik Mühendisliğine Giriş ve Kariyer Planlama | Mehmet Karadeniz | MFFB 501",
        "10:45–11:30 | MAT-17105 Matematik I | Asan Bayzakov | MFFB 301",
        "11:40–12:25 | MAT-17105 Matematik I | Asan Bayzakov | MFFB 301",
        "13:30–14:15 | RSC-103 Rusça I | Svetlana Parmanasova | MFFB Online",
        "14:25–15:10 | RSC-103 Rusça I | Svetlana Parmanasova | MFFB Online",
        "16:15–17:00 | ING-111 İngilizce I | Svetlana Çenebekova | MFFB Online",
        "17:10–17:55 | ING-111 İngilizce I | Svetlana Çenebekova | MFFB Online",
    ],
    "saturday": [],
    "sunday": [],
}
# ---- BIOLOGY (Biyoloji) ----
SCHED_BIOLOGY = {
    "monday": [
        "08:55–09:40 | BIO-103 Genel Zooloji | Bermet Kidiralieva | MFFB 221",
        "09:50–10:35 | BIO-103 Genel Zooloji | Bermet Kidiralieva | MFFB 223",
        "10:45–11:30 | MAT-110 Matematik I | Bakıtbay Ablabekov | MFFB 221",
        "11:40–12:25 | MAT-110 Matematik I | Bakıtbay Ablabekov | MFFB 221",
        "13:30–14:15 | BIO-103 Genel Zooloji | Bermet Kidiralieva | MFFB 223",
        "14:25–15:10 | BIO-103 Genel Zooloji | Bermet Kidiralieva | MFFB 223",
        "16:15–17:00 | BES-111(LFK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
        "17:10–17:55 | BIL-100 Enformatik | Çınara Cumabayeva | IIBF Online",
    ],
    "tuesday": [
        "10:45–11:30 | KMM-113(U) Genel Kimya (Uygulama) | Nurzat Şaykieva | MFFB 321",
        "11:40–12:25 | KMM-113(U) Genel Kimya (Uygulama) | Nurzat Şaykieva | MFFB 321",
        "13:30–14:15 | BIO-101 Genel Botanik | Miskalay Ganiyabayeva | MFFB 202",
        "14:25–15:10 | BIO-101 Genel Botanik | Miskalay Ganiyabayeva | MFFB 202",
        "15:20–16:05 | BIO-101 Genel Botanik | Miskalay Ganiyabayeva | MFFB 222",
        "16:15–17:00 | BIO-101 Genel Botanik | Miskalay Ganiyabayeva | MFFB 222",
        "17:10–17:55 | BES-111(LFK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
    ],
    "wednesday": [
        "10:45–11:30 | MAT-110 Matematik I | Bakıtbay Ablabekov | MFFB 202",
        "11:40–12:25 | MAT-110 Matematik I | Bakıtbay Ablabekov | MFFB 202",
        "13:30–14:15 | BES-111 Beden Eğitimi ve Spor I | Azamat Tillabayev | KSSB Spor sahası (kapalı)",
        "14:25–15:10 | BES-111 Beden Eğitimi ve Spor I | Azamat Tillabayev | KSSB Spor sahası (kapalı)",
        "16:15–17:00 | BES-111(LFK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
        "17:10–17:55 | BES-111(LFK) Beden Eğitimi ve Spor I (Sağlık Grubu) | Emiliya Bojirova | KSSB A-1",
    ],
    "thursday": [
        "10:45–11:30 | BIL-100 Enformatik | Çınara Cumabayeva | IIBF 111",
        "11:40–12:25 | BIL-100 Enformatik | Çınara Cumabayeva | IIBF 111",
        "16:15–17:00 | ING-111 İngilizce I | Svetlana Çenebekova | MFFB Online",
        "17:10–17:55 | ING-111 İngilizce I | Svetlana Çenebekova | MFFB Online",
    ],
    "friday": [
        "08:55–09:40 | BIO-105 Hücre Biyolojisi | Caynagül Isakova | MFFB 202",
        "09:50–10:35 | BIO-105 Hücre Biyolojisi | Caynagül Isakova | MFFB 202",
        "10:45–11:30 | BIO-105 Hücre Biyolojisi | Caynagül Isakova | MFFB 222",
        "11:40–12:25 | BIO-105 Hücre Biyolojisi | Caynagül Isakova | MFFB 222",
        "13:30–14:15 | KGZ-103 Kırgız Dili ve Edebiyatı I | Aynura Beyşeyeva | ZIRF 205",
        "14:25–15:10 | KGZ-103 Kırgız Dili ve Edebiyatı I | Aynura Beyşeyeva | ZIRF 205",
        "16:15–17:00 | RSC-103 Rusça I | Svetlana Parmanasova | MFFB Online",
        "17:10–17:55 | RSC-103 Rusça I | Svetlana Parmanasova | MFFB Online",
    ],
    "saturday": [],
    "sunday": [],
}


# Helpers to reuse for any department table (similar spirit to your _fmt_day_lines/_fmt_week)
def _weekday_key(dt: datetime) -> str:
    names = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
    return names[dt.weekday()]

def _fmt_day_from(table: dict[str, list[str]], dt: datetime, title: str) -> str:
    """
    Renders NON-management tables in the same visual style as Management:
      • <BOLD COURSE>  |  <code>HH:MM–HH:MM</code>
        <i>Teacher · Room</i>
    Accepts each lesson as a single string:
      "HH:MM–HH:MM | Course | Teacher | Room"
    """
    wk = _weekday_key(dt)
    rows = table.get(wk, [])
    if not rows:
        return f"📚 <b>{title}</b>\n\nНет пар на этот день."

    out = [f"📚 <b>{title}</b>\n"]
    for raw in rows:
        # Split exactly into 4 pieces
        parts = [p.strip() for p in raw.split("|")]
        # tolerate lines with extra pipes
        if len(parts) >= 4:
            time_s, course, teacher, room = parts[0], parts[1], parts[2], parts[3]
        else:
            # fallback: show raw line
            out.append(raw)
            out.append("")
            continue

        course_up = course.strip().upper()
        time_code = f"<code>{time_s.strip()}</code>"
        teacher_room = f"<i>{teacher.strip()} · {room.strip()}</i>"

        # two-line block like Management
        out.append(f"• <b>{course_up}</b>  {time_code}")
        out.append(teacher_room)
        out.append("")  # blank line between lessons

    return "\n".join(out).rstrip()

def _fmt_week_from(table: dict[str, list[str]], title: str) -> str:
    """
    Same visual style as Management, grouped by weekday.
    Expects the same per-row string format as _fmt_day_from.
    """
    order = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
    day_ru = {
        "monday": "Пн", "tuesday": "Вт", "wednesday": "Ср",
        "thursday": "Чт", "friday": "Пт", "saturday": "Сб", "sunday": "Вс"
    }
    blocks: list[str] = [f"📅 <b>{title}</b> — вся неделя\n"]

    for k in order:
        rows = table.get(k, [])
        blocks.append(f"<b>{day_ru[k]}</b>")
        if not rows:
            blocks.append("—")
            blocks.append("")
            continue

        for raw in rows:
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) >= 4:
                time_s, course, teacher, room = parts[0], parts[1], parts[2], parts[3]
                course_up = course.upper()
                time_code = f"<code>{time_s}</code>"
                blocks.append(f"• <b>{course_up}</b>  {time_code}")
                blocks.append(f"<i>{teacher} · {room}</i>")
                blocks.append("")
            else:
                blocks.append(raw)
                blocks.append("")

    return "\n".join(blocks).rstrip()

def _schedule_text_for(dept_key: str, day_key: str, now: datetime) -> str:
    if dept_key == "management":
        # use your existing pretty output for Management
        if day_key == "today":
            return _fmt_day_lines(now)
        elif day_key == "tomorrow":
            return _fmt_day_lines(now + timedelta(days=1))
        elif day_key == "week":
            return _fmt_week(now)
        return "Неизвестный период."

    # Static tables for other departments
    if dept_key == "programming":
        title = f"{DEPT_LABEL['programming']}"
        if day_key == "today":
            return _fmt_day_from(SCHED_PROGRAMMING, now, title)
        elif day_key == "tomorrow":
            return _fmt_day_from(SCHED_PROGRAMMING, now + timedelta(days=1), title)
        elif day_key == "week":
            return _fmt_week_from(SCHED_PROGRAMMING, title)
        return "Неизвестный период."

    if dept_key == "electrical":
        title = f"{DEPT_LABEL['electrical']}"
        if day_key == "today":
            return _fmt_day_from(SCHED_ELECTRICAL, now, title)
        elif day_key == "tomorrow":
            return _fmt_day_from(SCHED_ELECTRICAL, now + timedelta(days=1), title)
        elif day_key == "week":
            return _fmt_week_from(SCHED_ELECTRICAL, title)
        return "Неизвестный период."
    if dept_key == "biology":
        title = DEPT_LABEL["biology"]
        if day_key == "today":
            return _fmt_day_from(SCHED_BIOLOGY, now, title)
        elif day_key == "tomorrow":
            return _fmt_day_from(SCHED_BIOLOGY, now + timedelta(days=1), title)
        elif day_key == "week":
            return _fmt_week_from(SCHED_BIOLOGY, title)
        return "Неизвестный период."


    return "Неизвестная кафедра."

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📚 Расписание → выберите кафедру:",
        reply_markup=kb_departments(),
        parse_mode=ParseMode.HTML,
    )


async def schedule_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = (q.data or "")
    now = datetime.now(BISHKEK_TZ)

    # Step 1: department -> show day choices
    if data.startswith("sch:dept:"):
        dept_key = data.split(":", 2)[2]
        pretty = DEPT_LABEL.get(dept_key, dept_key)
        await q.edit_message_text(
            text=f"Кафедра: <b>{pretty}</b>\nТеперь выберите период:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_days(dept_key),
        )
        return

    # Step 2: day -> show schedule
    if data.startswith("sch:day:"):
        _, _, dept_key, day_key = data.split(":", 3)
        text = _schedule_text_for(dept_key, day_key, now)

        # For long week output you can send as a fresh message (optional)
        if day_key == "week" and dept_key == "management":
            try:
                await q.edit_message_text(f"📅 {DEPT_LABEL.get(dept_key, dept_key)} — вся неделя:")
            except Exception:
                pass
            await context.bot.send_message(
                chat_id=q.message.chat_id, text=text, parse_mode=ParseMode.HTML
            )
        else:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML)
        return

    # Back-compat if old buttons are clicked
    if data in ("sch:today", "sch:tomorrow", "sch:week"):
        await q.edit_message_text("Обновлено: сначала выберите кафедру через /schedule")
        return





# ======================= ADDED COMMANDS 
# ===================== QOTD & COINFLIP =====================
import random
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes

# --- Quote of the Day ---
_QOTD_LOCAL = [
    ("Чем умнее человек, тем легче он признает себя дураком.", "Альберт Эйнштейн"),
    ("Никогда не ошибается тот, кто ничего не делает.", "Теодор Рузвельт"),
    ("Менее всего просты люди, желающие казаться простыми.", "Лев Николаевич Толстой"),
    ("Мы находимся здесь, чтобы внести свой вклад в этот мир. Иначе зачем мы здесь?", "Стив Джобс"),
    ("Мода проходит, стиль остаётся.", "Коко Шанель"),
    ("Если человек не нашёл, за что может умереть, он не способен жить.", "Мартин Лютер Кинг"),
    ("Музыка заводит сердца так, что пляшет и поёт тело. А есть музыка, с которой хочется поделиться всем, что наболело.", "Джон Леннон"),
    ("Если кто-то причинил тебе зло, не мсти. Сядь на берегу реки, и вскоре ты увидишь, как мимо тебя проплывает труп твоего врага.", "Лао-цзы"),
    ("Лучше быть хорошим человеком, 'ругающимся матом', чем тихой, воспитанной тварью.", "Фаина Раневская"),
    ("Если тебе тяжело, значит ты поднимаешься в гору. Если тебе легко, значит ты летишь в пропасть.", "Генри Форд"),
    ("Мой способ шутить – это говорить правду. На свете нет ничего смешнее.", "Бернард Шоу"),
    ("Чем больше любви, мудрости, красоты, доброты вы откроете в самом себе, тем больше вы заметите их в окружающем мире.", "Мать Тереза"),
    ("Единственный человек, с которым вы должны сравнивать себя, – это вы в прошлом. И единственный человек, лучше которого вы должны быть, – это вы сейчас.", "Зигмунд Фрейд"),
    ("Невозможность писать для меня равносильна погребению заживо...", "Михаил Булгаков"),
    ("История – самый лучший учитель, у которого самые плохие ученики.", "Индира Ганди"),
    ("Дай человеку власть, и ты узнаешь, кто он.", "Наполеон Бонапарт"),
    ("Ядерную войну невозможно выиграть.", "Андрей Сахаров"),
    ("Поражение? Я не понимаю значения этого слова.", "Маргарет Тэтчер"),
    ("Некоторые люди проводят жизнь в поисках любви вне их самих... Пока любовь в моём сердце, она повсюду.", "Майкл Джексон"),
    ("Человечество обладает одним поистине мощным оружием, и это смех.", "Марк Твен"),
    ("Тренируйся с теми, кто сильнее. Не сдавайся там, где сдаются другие. И победишь там, где победить нельзя.", "Брюс Ли"),
    ("Комедия – это очень серьёзное дело!", "Юрий Никулин"),
    ("Будьте менее любопытны о людях, но более любопытны об идеях.", "Мария Кюри"),
    ("Когда я собираюсь писать новый сценарий, самое трудное для меня – это пойти в канцтовары и купить блокнот.", "Квентин Тарантино"),
    ("Ненавижу всяческую мертвечину! Обожаю всяческую жизнь!", "Владимир Маяковский"),
    ("Мышление – верх блаженства и радость жизни, доблестнейшее занятие человека.", "Аристотель"),
    ("У тебя есть враги? Хорошо. Значит, в своей жизни ты что-то когда-то отстаивал.", "Уинстон Черчилль"),
    ("Когда-нибудь не страшно умереть – страшно умереть вот сейчас.", "Александр Солженицын"),
    ("Я серьёзно отношусь к своей работе, а это возможно только при несерьёзном отношении к собственной персоне.", "Алан Рикман"),
    ("Характер – это и есть судьба.", "Майя Плисецкая"),
    ("Внимай лишь одному учителю – Природе.", "Рембрандт"),
    ("Успех – паршивый учитель. Он заставляет умных людей думать, что они не могут проиграть.", "Билл Гейтс"),
    ("Чемпионами становятся не в тренажёрных залах. Чемпиона рождает то, что у человека внутри: желания, мечты, цели.", "Мухаммед Али"),
    ("Люди – слишком сложные существа, чтобы понять их полностью.", "Том Хэнкс"),
    ("Перспектива рано умереть заставила меня понять, что жизнь стоит того, чтобы её прожить.", "Стивен Хокинг"),
    ("Не так уж сложно изменить общество – сложно изменить себя.", "Нельсон Мандела"),
    ("Необходимо, чтобы художник, кроме глаза, воспитывал и свою душу.", "Василий Кандинский"),
    ("Я дышу, и значит – я люблю! Я люблю, и значит – я живу!", "Владимир Высоцкий"),
    ("Фантазия мужчины – лучшее оружие женщины.", "Софи Лорен"),
    ("То, что мы знаем, это капля, а то, что мы не знаем, это океан.", "Исаак Ньютон"),
    ("Ни высокий интеллект, ни воображение, ни то и другое вместе не творят гения. Любовь, любовь и любовь – вот в чём сущность гения.", "Вольфганг Амадей Моцарт"),
    ("Оправдайте, не карайте, но назовите зло злом.", "Фёдор Достоевский"),
    ("Не оборачивается тот, кто устремлён к звёздам.", "Леонардо да Винчи"),
    ("Ненавижу советы – все, кроме своих.", "Джек Николсон"),
    ("Красота женщины множится вместе с её годами.", "Одри Хепберн"),
    ("Шире открой глаза, живи так жадно, как будто через десять секунд умрёшь. Старайся увидеть мир. Он прекраснее любой мечты, созданной на фабрике и оплаченной деньгами. Не проси гарантий, не ищи покоя – такого зверя нет на свете.", "Рэй Брэдбери"),
    ("Я пью, чтобы окружающие меня люди становились интереснее.", "Эрнест Хемингуэй"),
    ("Видите ли, художника отличает то, что в его жизни бывают минуты, когда он ощущает себя больше чем человеком.", "Ле Корбюзье"),
    ("Любовь к собственному благу производит в нас любовь к отечеству, а личное самолюбие – гордость народную, которая служит опорою патриотизма.", "Николай Карамзин"),
    ("Любите искусство в себе, а не себя в искусстве.", "Константин Станиславский"),
]


def _pick_qotd() -> tuple[str, str]:
    """Return a deterministic quote for today."""
    seed = datetime.utcnow().strftime("%Y-%m-%d")
    rng = random.Random(seed)
    return rng.choice(_QOTD_LOCAL)

async def qotd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the quote of the day."""
    quote, author = _pick_qotd()
    await update.effective_message.reply_text(
        f"💬 *Quote of the Day*\n\n“{quote}”\n— {author}",
        parse_mode="Markdown"
    )

# --- Coin Flip ---
async def coinflip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Flip a coin."""
    side = random.choice(["🪙 Решка", "🪙 Орёл"])
    await update.effective_message.reply_text(side)
# ===================== END QOTD & COINFLIP =====================

# ===================== /SECRET COMMAND =====================
# Secure ephemeral messaging for groups
# Paste this entire block into your bot (15).py or bot (16).py

import hashlib
import hmac
import secrets
import json
from datetime import datetime, timedelta
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

# --- Configuration ---
SECRET_HMAC_KEY = secrets.token_bytes(32)  # Generate once per bot session
SECRET_TTL_MINUTES = 30
SECRET_MAX_ALERT_LEN = 200

# --- In-Memory Store ---
_SECRET_STORE: dict[str, dict] = {}
# Structure: {
#   "secret_id": {
#     "recipient_id": int,
#     "secret": str,
#     "expires_at": datetime,
#     "sender_name": str
#   }
# }

def _generate_secret_id() -> str:
    """Generate a short, unique secret ID."""
    return secrets.token_urlsafe(12)

def _create_hmac_token(secret_id: str, recipient_id: int) -> str:
    """Generate HMAC token for deep-link validation."""
    data = f"{secret_id}:{recipient_id}".encode()
    return hmac.new(SECRET_HMAC_KEY, data, hashlib.sha256).hexdigest()[:32]

def _validate_hmac_token(secret_id: str, recipient_id: int, token: str) -> bool:
    """Validate HMAC token."""
    expected = _create_hmac_token(secret_id, recipient_id)
    return hmac.compare_digest(expected, token)

def _cleanup_expired():
    """Remove expired secrets."""
    now = datetime.now()
    expired = [sid for sid, data in _SECRET_STORE.items() if data["expires_at"] < now]
    for sid in expired:
        del _SECRET_STORE[sid]

def create_secret(recipient_id: int, text: str, sender_name: str) -> tuple[str, str, bool, str]:
    """
    Store a secret and return (secret_id, truncated_text, needs_dm, token).
    
    Returns:
        - secret_id: Unique identifier
        - truncated_text: Text for alert (max 200 chars)
        - needs_dm: True if text exceeds alert limit
        - token: HMAC token for deep-link
    """
    _cleanup_expired()
    
    secret_id = _generate_secret_id()
    expires_at = datetime.now() + timedelta(minutes=SECRET_TTL_MINUTES)
    
    _SECRET_STORE[secret_id] = {
        "recipient_id": recipient_id,
        "secret": text,
        "expires_at": expires_at,
        "sender_name": sender_name
    }
    
    # Truncate if needed
    needs_dm = len(text) > SECRET_MAX_ALERT_LEN
    truncated = text[:SECRET_MAX_ALERT_LEN] + "…" if needs_dm else text
    
    token = _create_hmac_token(secret_id, recipient_id)
    
    return secret_id, truncated, needs_dm, token

def get_secret(secret_id: str) -> Optional[dict]:
    """Retrieve secret if exists and not expired."""
    _cleanup_expired()
    return _SECRET_STORE.get(secret_id)

# --- Command Handlers ---

async def secret_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /secret @username your secret message
    OR reply to someone with: /secret your secret message
    """
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    
    if not msg or not user:
        return
    
    # Only works in groups
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("🔐 /secret works only in groups.")
        return
    
    # Parse args
    args = context.args or []
    text = msg.text or ""
    
    # Determine recipient
    recipient_id = None
    recipient_username = None
    secret_text = None
    
    # Option 1: Reply mode
    if msg.reply_to_message and msg.reply_to_message.from_user:
        recipient = msg.reply_to_message.from_user
        recipient_id = recipient.id
        recipient_username = recipient.username or recipient.full_name
        # Secret is everything after /secret
        secret_text = " ".join(args) if args else None
    
    # Option 2: @mention mode
    elif args and args[0].startswith("@"):
        recipient_username = args[0].lstrip("@")
        secret_text = " ".join(args[1:]) if len(args) > 1 else None
        
        # Try to find user ID from entities
        for entity in (msg.entities or []):
            if entity.type == "mention":
                # Can't get ID from plain @mention, will need manual lookup
                # For now, we'll use username only
                pass
            elif entity.type == "text_mention" and entity.user:
                recipient_id = entity.user.id
                recipient_username = entity.user.username or entity.user.full_name
                break
    
    # Validation
    if not recipient_username:
        await msg.reply_text("❌ Reply to someone or use: /secret @username your message")
        return
    
    if not secret_text or len(secret_text.strip()) == 0:
        await msg.reply_text("❌ Secret message cannot be empty.")
        return
    
    # Don't allow self-secrets
    if recipient_id and recipient_id == user.id:
        await msg.reply_text("❌ You can't send secrets to yourself.")
        return
    
    # If we don't have recipient_id (plain @mention), try to resolve it
    if not recipient_id:
        try:
            # Try to get chat member by username
            # Note: This may not always work due to Telegram API limitations
            member = await context.bot.get_chat_member(chat.id, f"@{recipient_username}")
            recipient_id = member.user.id
        except Exception:
            # Fallback: store with username only (less secure but functional)
            # For production, you might want to require reply or text_mention
            await msg.reply_text(f"⚠️ Couldn't verify @{recipient_username}. Reply to their message instead.")
            return
    
    # Create secret
    sender_name = user.full_name or user.username or "Someone"
    secret_id, truncated, needs_dm, token = create_secret(
        recipient_id, secret_text, sender_name
    )
    
    # Build inline keyboard
    buttons = [[InlineKeyboardButton("👀 Reveal", callback_data=f"sc|{secret_id}")]]
    
    if needs_dm:
        bot_username = (await context.bot.get_me()).username
        deep_link = f"https://t.me/{bot_username}?start={secret_id}_{token}"
        buttons.append([InlineKeyboardButton("✉️ Read in DM", url=deep_link)])
    
    keyboard = InlineKeyboardMarkup(buttons)
    
    # Send public message
    await msg.reply_text(
        f"🔐 Secret for @{recipient_username} — only they can tap to view",
        reply_markup=keyboard
    )
    
    # Delete original command (optional, for privacy)
    try:
        await msg.delete()
    except Exception:
        pass

async def secret_reveal_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle 'Reveal' button tap."""
    q = update.callback_query
    await q.answer(cache_time=0)  # Acknowledge immediately
    
    if not q or not q.from_user:
        return
    
    try:
        # Parse callback data: sc|<secret_id>
        _, secret_id = q.data.split("|", 1)
    except Exception:
        await q.answer("❌ Invalid secret.", show_alert=True)
        return
    
    # Retrieve secret
    secret_data = get_secret(secret_id)
    
    if not secret_data:
        await q.answer("❌ Secret expired or not found.", show_alert=True)
        return
    
    # Check if tapper is the intended recipient
    if q.from_user.id != secret_data["recipient_id"]:
        await q.answer("🚫 This secret isn't for you.", show_alert=True)
        return
    
    # Reveal secret (ephemeral alert)
    secret_text = secret_data["secret"]
    sender_name = secret_data["sender_name"]
    
    # Truncate if too long for alert
    if len(secret_text) > SECRET_MAX_ALERT_LEN:
        display_text = secret_text[:SECRET_MAX_ALERT_LEN] + "…\n\n[Tap 'Read in DM' for full message]"
    else:
        display_text = secret_text
    
    await q.answer(
        f"🔓 From {sender_name}:\n\n{display_text}",
        show_alert=True
    )

async def start_with_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /start <token> for reading full secrets in DM.
    Token format: <secret_id>_<hmac>
    """
    msg = update.effective_message
    user = update.effective_user
    
    if not msg or not user:
        return
    
    # Only process in private chats
    if update.effective_chat.type != "private":
        return
    
    # Check if this is a secret deep-link
    if not context.args or not context.args[0]:
        return
    
    token_str = context.args[0]
    
    # Parse token: secret_id_hmac
    try:
        secret_id, hmac_token = token_str.split("_", 1)
    except ValueError:
        # Not a secret token, ignore (could be other /start usage)
        return
    
    # Retrieve secret
    secret_data = get_secret(secret_id)
    
    if not secret_data:
        await msg.reply_text("❌ Secret expired or not found.")
        return
    
    # Validate HMAC
    if not _validate_hmac_token(secret_id, secret_data["recipient_id"], hmac_token):
        await msg.reply_text("❌ Invalid secret link.")
        return
    
    # Check if opener is the intended recipient
    if user.id != secret_data["recipient_id"]:
        await msg.reply_text("🚫 This secret isn't for you.")
        return
    
    # Send full secret in DM
    secret_text = secret_data["secret"]
    sender_name = secret_data["sender_name"]
    
    # Split into chunks if very long (Telegram limit: 4096)
    max_len = 4000
    if len(secret_text) <= max_len:
        await msg.reply_text(f"🔓 *Secret from {sender_name}:*\n\n{secret_text}", parse_mode=ParseMode.MARKDOWN)
    else:
        await msg.reply_text(f"🔓 *Secret from {sender_name}:*", parse_mode=ParseMode.MARKDOWN)
        for i in range(0, len(secret_text), max_len):
            chunk = secret_text[i:i+max_len]
            await msg.reply_text(chunk)

# ===================== END /SECRET COMMAND =====================
def _load_font(size: int) -> ImageFont.FreeTypeFont:
    # Try DejaVu (bundled with Pillow). Fallback to default bitmap font.
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except Exception:
        return ImageFont.load_default()

def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, content_width: int) -> str:
    """
    Wrap so each line aims for 6 words (never fewer than 5 if it fits),
    while still respecting the available pixel width.
    """
    TARGET = 6
    MINLINE = 5

    words = text.split()
    if not words:
        return ""

    lines = []
    i = 0
    while i < len(words):
        # try to take up to TARGET words
        max_take = min(TARGET, len(words) - i)

        # try the largest count first (TARGET), down to MINLINE, that fits in width
        took = 0
        for take in range(max_take, MINLINE - 1, -1):
            candidate = " ".join(words[i:i+take])
            if draw.textlength(candidate, font=font) <= content_width:
                lines.append(candidate)
                i += take
                took = take
                break

        if took:
            continue

        # If even MINLINE words don't fit, fall back to the longest that fits (>=1 word)
        # This covers very long words or narrow content width.
        take = 1
        while i + take <= len(words):
            candidate = " ".join(words[i:i+take])
            if draw.textlength(candidate, font=font) > content_width:
                break
            take += 1
        # take-1 fits, or if nothing fit, just take one word
        take = max(1, take - 1)
        lines.append(" ".join(words[i:i+take]))
        i += take

    return "\n".join(lines)


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
        html = await fetch_menu_html_async()   # NEW
        menu = await parse_menu_async(html)    # NEW
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

   
# ===================== ENHANCED STICKER QUOTE =====================
# Replace your existing stickerquote function with this enhanced version

from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter
from telegram import Update
from telegram.ext import ContextTypes
import asyncio

# --- Configuration ---
STICKER_MAX_SIZE = 512  # Telegram sticker size limit
AVATAR_SIZE = 350
TEXT_PADDING = 56
BUBBLE_RADIUS = 42
LINE_SPACING = 18

# Colors
BG_DARK = (18, 18, 18, 255)
BUBBLE_DARK = (34, 34, 34, 255)
NAME_COLOR = (170, 152, 255, 255)
TEXT_COLOR = (245, 245, 245, 255)
META_COLOR = (200, 200, 200, 255)
OVERLAY_BG = (0, 0, 0, 180)  # Semi-transparent black for image overlays

# --- Helper Functions ---

def _load_font_safe(size: int) -> ImageFont.FreeTypeFont:
    """Load DejaVuSans font with fallback."""
    try:
        font_path = Path(__file__).parent / "fonts" / "DejaVuSans.ttf"
        if font_path.exists():
            return ImageFont.truetype(str(font_path), size=size)
    except Exception:
        pass
    
    # Try system fonts
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    
    return ImageFont.load_default()


def _wrap_text_smart(draw: ImageDraw.ImageDraw, text: str, 
                     font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Wrap text to fit within max_width."""
    words = text.split()
    if not words:
        return ""
    
    lines = []
    current_line = words[0]
    
    for word in words[1:]:
        test_line = f"{current_line} {word}"
        if draw.textlength(test_line, font=font) <= max_width:
            current_line = test_line
        else:
            lines.append(current_line)
            current_line = word
    
    lines.append(current_line)
    return "\n".join(lines)


async def _fetch_avatar(bot, user, size: int) -> Image.Image:
    """Fetch user avatar as circular image with fallback."""
    try:
        photos = await bot.get_user_profile_photos(user_id=user.id, limit=1)
        if photos.total_count > 0:
            file = await bot.get_file(photos.photos[0][-1].file_id)
            avatar_bytes = await file.download_as_bytearray()
            img = Image.open(BytesIO(avatar_bytes)).convert("RGBA")
            
            # Crop to square and resize
            img = ImageOps.fit(img, (size, size), method=Image.LANCZOS, centering=(0.5, 0.5))
            
            # Create circular mask
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
            img.putalpha(mask)
            return img
    except Exception as e:
        print(f"Avatar fetch failed: {e}")
    
    # Fallback: create colored circle with initials
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, size - 1, size - 1), fill=(96, 96, 160, 255))
    
    # Add initials
    initials = ""
    if user.first_name:
        initials += user.first_name[0]
    if user.last_name:
        initials += user.last_name[0]
    initials = initials.strip().upper() or "?"
    
    font = _load_font_safe(int(size * 0.45))
    bbox = draw.textbbox((0, 0), initials, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    draw.text(
        ((size - text_width) / 2, (size - text_height) / 2 - 2),
        initials,
        font=font,
        fill=(255, 255, 255, 255)
    )
    
    return img


def _create_text_sticker(avatar: Image.Image, display_name: str, 
                         handle: str, text: str) -> Image.Image:
    """Create a text-based quote sticker."""
    W = 2000
    PAD = 56
    AV = AVATAR_SIZE
    
    # Load fonts
    font_name = _load_font_safe(140)
    font_meta = _load_font_safe(90)
    font_text = _load_font_safe(120)
    
    # Create base image
    temp_img = Image.new("RGBA", (W, 10), BG_DARK)
    temp_draw = ImageDraw.Draw(temp_img)
    
    # Calculate layout
    x_text = PAD + AV + 40
    y_top = PAD
    
    # Name and handle dimensions
    name_bbox = temp_draw.textbbox((0, 0), display_name, font=font_name)
    name_h = name_bbox[3] - name_bbox[1]
    
    handle_h = 0
    if handle:
        handle_bbox = temp_draw.textbbox((0, 0), handle, font=font_meta)
        handle_h = handle_bbox[3] - handle_bbox[1]
    
    # Bubble dimensions
    bubble_w = W - PAD - x_text
    inner_pad = TEXT_PADDING
    content_w = bubble_w - inner_pad * 2
    
    # Wrap text
    wrapped = _wrap_text_smart(temp_draw, text, font_text, content_w)
    text_bbox = temp_draw.multiline_textbbox((0, 0), wrapped, font=font_text, spacing=LINE_SPACING)
    text_h = text_bbox[3] - text_bbox[1]
    bubble_h = text_h + inner_pad * 2
    
    # Calculate bubble position
    gap_name = 20
    by = y_top + name_h + (handle_h if handle else 0) + gap_name
    
    # Calculate final height
    H = max(by + bubble_h + PAD, PAD + AV + PAD)
    
    # Create final image
    img = Image.new("RGBA", (W, H), BG_DARK)
    draw = ImageDraw.Draw(img)
    
    # Paste avatar
    img.paste(avatar, (PAD, y_top), avatar)
    
    # Draw name
    draw.text((x_text, y_top), display_name, font=font_name, fill=NAME_COLOR)
    
    # Draw handle if present
    if handle and handle != display_name:
        name_w = draw.textlength(display_name + "  ", font=font_name)
        draw.text((x_text + name_w, y_top + 12), handle, font=font_meta, fill=META_COLOR)
    
    # Draw bubble
    bubble = Image.new("RGBA", (bubble_w, bubble_h), (0, 0, 0, 0))
    bubble_draw = ImageDraw.Draw(bubble)
    bubble_draw.rounded_rectangle(
        (0, 0, bubble_w, bubble_h),
        radius=BUBBLE_RADIUS,
        fill=BUBBLE_DARK
    )
    img.paste(bubble, (x_text, by), bubble)
    
    # Draw text (centered vertically in bubble)
    y_text = by + (bubble_h - text_h) // 2
    draw.multiline_text(
        (x_text + inner_pad, y_text),
        wrapped,
        font=font_text,
        fill=TEXT_COLOR,
        spacing=LINE_SPACING
    )
    
    return img


def _create_image_overlay_sticker(base_image: Image.Image, avatar: Image.Image,
                                  display_name: str, handle: str, text: str) -> Image.Image:
    """Create a quote sticker by overlaying text on an existing image."""
    # Resize base image if too large
    max_dim = 2000
    if max(base_image.size) > max_dim:
        ratio = max_dim / max(base_image.size)
        new_size = tuple(int(dim * ratio) for dim in base_image.size)
        base_image = base_image.resize(new_size, Image.LANCZOS)
    
    img = base_image.convert("RGBA")
    W, H = img.size
    
    # Create semi-transparent overlay at bottom
    overlay_height = min(H // 2, 800)
    overlay = Image.new("RGBA", (W, overlay_height), OVERLAY_BG)
    
    # Apply gradient effect for smoother transition
    for y in range(overlay_height):
        alpha = int(180 * (y / overlay_height))
        for x in range(W):
            r, g, b, _ = overlay.getpixel((x, y))
            overlay.putpixel((x, y), (r, g, b, alpha))
    
    # Paste overlay at bottom
    img.paste(overlay, (0, H - overlay_height), overlay)
    
    # Add avatar (smaller for overlay)
    av_size = min(AVATAR_SIZE // 2, 200)
    avatar_small = avatar.resize((av_size, av_size), Image.LANCZOS)
    av_x = 30
    av_y = H - overlay_height + 30
    img.paste(avatar_small, (av_x, av_y), avatar_small)
    
    # Load fonts (smaller for overlay)
    font_name = _load_font_safe(80)
    font_meta = _load_font_safe(50)
    font_text = _load_font_safe(70)
    
    draw = ImageDraw.Draw(img)
    
    # Text positioning
    text_x = av_x + av_size + 20
    text_y = av_y
    
    # Draw name
    draw.text((text_x, text_y), display_name, font=font_name, fill=TEXT_COLOR)
    
    # Draw handle
    if handle and handle != display_name:
        name_bbox = draw.textbbox((0, 0), display_name, font=font_name)
        name_h = name_bbox[3] - name_bbox[1]
        draw.text((text_x, text_y + name_h + 5), handle, font=font_meta, fill=META_COLOR)
        text_y += name_h + 50
    else:
        name_bbox = draw.textbbox((0, 0), display_name, font=font_name)
        name_h = name_bbox[3] - name_bbox[1]
        text_y += name_h + 30
    
    # Wrap and draw message text
    max_text_w = W - text_x - 30
    wrapped = _wrap_text_smart(draw, text, font_text, max_text_w)
    draw.multiline_text(
        (text_x, text_y),
        wrapped,
        font=font_text,
        fill=TEXT_COLOR,
        spacing=12
    )
    
    return img


def _resize_for_sticker(img: Image.Image) -> Image.Image:
    """Resize image to Telegram sticker dimensions (max 512px)."""
    w, h = img.size
    max_side = STICKER_MAX_SIZE
    
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        new_size = (int(w * scale), int(h * scale))
        img = img.resize(new_size, Image.LANCZOS)
    
    return img


# --- Main Command ---

async def stickerquote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Create a sticker-style quote from a message.
    
    Usage:
    - Reply to any message with /stickerquote
    - For text messages: creates a styled text sticker
    - For images: overlays the quote on the image
    """
    msg = update.effective_message
    bot = context.bot
    
    # Determine source message
    target = msg.reply_to_message
    if not target:
        await msg.reply_text(
            "❌ Please reply to a message with /stickerquote\n\n"
            "Supported:\n"
            "• Text messages → styled sticker\n"
            "• Photos → overlay quote on image"
        )
        return
    
    author = target.from_user
    if not author:
        await msg.reply_text("❌ Cannot quote messages from channels or anonymous admins.")
        return
    
    # Extract text
    text_to_quote = target.text or target.caption or ""
    if not text_to_quote:
        await msg.reply_text("❌ The replied message has no text to quote.")
        return
    
    # Truncate if too long
    if len(text_to_quote) > 500:
        text_to_quote = text_to_quote[:497] + "..."
    
    # Get author info
    display_name = author.full_name or f"@{author.username}" or "Unknown"
    handle = f"@{author.username}" if author.username else None
    
    # Send "processing" indicator
    status_msg = await msg.reply_text("🎨 Creating sticker...")
    
    try:
        # Fetch avatar
        avatar = await _fetch_avatar(bot, author, AVATAR_SIZE)
        
        # Check if target has a photo
        has_photo = False
        base_image = None
        
        if target.photo:
            # Download the largest photo
            photo = target.photo[-1]
            file = await bot.get_file(photo.file_id)
            photo_bytes = await file.download_as_bytearray()
            base_image = Image.open(BytesIO(photo_bytes)).convert("RGBA")
            has_photo = True
        
        # Create sticker
        if has_photo and base_image:
            final_img = _create_image_overlay_sticker(
                base_image, avatar, display_name, handle or "", text_to_quote
            )
        else:
            final_img = _create_text_sticker(
                avatar, display_name, handle or "", text_to_quote
            )
        
        # Resize for Telegram
        final_img = _resize_for_sticker(final_img)
        
        # Convert to WEBP
        output = BytesIO()
        output.name = "quote.webp"
        final_img.save(output, format="WEBP", quality=95, method=6)
        output.seek(0)
        
        # Send sticker
        await bot.send_sticker(chat_id=update.effective_chat.id, sticker=output)
        
        # Delete status message
        try:
            await status_msg.delete()
        except Exception:
            pass
            
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to create sticker: {str(e)}")
        print(f"Stickerquote error: {e}")
        import traceback
        traceback.print_exc()


# --- Integration Instructions ---
# In your main() function, add this handler:
# app.add_handler(CommandHandler(["stickerquote", "sq"], stickerquote))

# Add this in main():
# app.add_handler(CommandHandler("stickerquote", stickerquote))

import random
from telegram import Update
from telegram.ext import ContextTypes

def _display_name_for(user) -> str:
    """Берём имя по user_id из карты REAL_NAMES, иначе читаемое запасное имя."""
    if user and user.id in REAL_NAMES:
        return REAL_NAMES[user.id]
    # запасной вариант — нормально отображаемое имя
    return user.full_name or (user.username and f"@{user.username}") or "Кто-то"

def _pick_prediction_for(user_id: int) -> str:
    """Случайно выбираем предсказание, стараясь не повторять последнее для этого пользователя."""
    if not PREDICTIONS_RU:
        return "Предсказания ещё не настроены."
    # избегаем мгновенного повтора
    banned = set(_LAST_PICKS[user_id])
    choices = [i for i in range(len(PREDICTIONS_RU)) if i not in banned] or list(range(len(PREDICTIONS_RU)))
    idx = random.choice(choices)
    _LAST_PICKS[user_id].append(idx)
    return PREDICTIONS_RU[idx]

async def predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Использование:
      • Просто /predict — предсказание для себя.
      • Ответом на сообщение /predict — предсказание для автора того сообщения.
      • /predict <user_id> — если знаешь id (по желанию).
    """
    msg = update.effective_message
    target_user = None

    # 1) Если ответили на чьё-то сообщение — предсказываем для него
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_user = msg.reply_to_message.from_user

    # 2) Иначе попробуем взять id из аргумента
    if not target_user and context.args:
        try:
            uid = int(context.args[0])
            member = await context.bot.get_chat_member(msg.chat_id, uid)
            target_user = member.user
        except Exception:
            # не смогли — игнорируем аргумент
            pass

    # 3) По умолчанию — для самого вызвавшего
    if not target_user:
        target_user = update.effective_user

    real_name = _display_name_for(target_user)
    text = _pick_prediction_for(target_user.id)
    await msg.reply_html(f"🔮 <b>{real_name}</b>\n{text}")




CROC_CB_PREFIX = "croc:"  # make sure your button callback_data starts with this

async def croc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return

    # Expected format: "croc:<action>:<chat_id>:<explainer_id>"
    try:
        _, action, chat_id_s, explainer_id_s = q.data.split(":")
        chat_id = int(chat_id_s)
        explainer_id = int(explainer_id_s)
    except Exception:
        return

    # Only the explainer may use these buttons
    if not q.from_user or q.from_user.id != explainer_id:
        await q.answer("Только объясняющий может пользоваться этими кнопками.", show_alert=True)
        return

    g = CROC_GAMES.get(chat_id)
    if not g or g.get("explainer_id") != explainer_id:
        await q.answer("Раунд уже не активен.", show_alert=True)
        return

    if action == "show":
        await q.answer(text=f"ТВОЁ СЛОВО:\n\n{g['word']}", show_alert=True)
        return

    if action == "skip":
        new_word = _croc_pick_word(chat_id)
        g["word"] = new_word
        g["used"].add(new_word)
        await q.answer(text=f"НОВОЕ СЛОВО:\n\n{new_word}", show_alert=True)
        # optional: keep the same markup; no need to edit text
        try:
            await q.edit_message_reply_markup(reply_markup=q.message.reply_markup)
        except Exception:
            pass
        return

    if action == "end":
        CROC_GAMES.pop(chat_id, None)
        await q.answer("Раунд завершён.", show_alert=True)
        try:
            await q.message.reply_text("🛑 Раунд завершён организатором.")
        except Exception:
            pass
        return





# =======================
# MAIN
# =======================
def main():
    # Load Croc scores once
    _croc_load_scores()

    # --- App / scheduler ---
    scheduler = AsyncIOScheduler(timezone=BISHKEK_TZ)
    job_queue = JobQueue()
    job_queue.scheduler = scheduler

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .job_queue(job_queue)
        .build()
    )

    # =========================
    # Command handlers (your existing ones)
    # =========================
    app.add_handler(
        CommandHandler(
            ["quote", "q"],
            quote,
            filters=filters.ChatType.PRIVATE | filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP,
        )
    )
    # app.add_handler(
    #     CommandHandler(
             #["qshot", "qimg", "quoteimg"],
           #  qshot,
         #    filters=filters.ChatType.PRIVATE | filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP,
       #  )
     #)
    app.add_handler(CommandHandler("yemek", yemek))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CommandHandler(["say", "echo"], say))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler(["schedule", "sch"], schedule_cmd))


    # =========================
    # Crocodile (PUT BEFORE generic callbacks/text handlers)
    # =========================
    app.add_handler(CommandHandler("croc", croc_cmd))
    
    app.add_handler(CommandHandler("rating", croc_rating))
    app.add_handler(CallbackQueryHandler(croc_callback, pattern=r"^croc:"))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            croc_group_listener,
        )
    )

    # =========================
    # Your generic callback handler (must be AFTER croc_callback above)
    # =========================
    app.add_handler(CommandHandler("secret", secret_cmd))
    app.add_handler(CallbackQueryHandler(secret_reveal_cb, pattern=r"^sc\|"))
    app.add_handler(CallbackQueryHandler(schedule_cb, pattern=r"^sch:"))
    app.add_handler(CallbackQueryHandler(button))

    # =========================
    # Remaining handlers (keep as needed)
    # =========================

    # Add to existing /start handler or create new one:
    app.add_handler(CommandHandler("start", start_with_token))
    app.add_handler(CommandHandler("qotd", qotd))
    app.add_handler(CommandHandler("coinflip", coinflip))
    app.add_handler(CommandHandler("predict", predict))
    app.add_handler(CommandHandler("stickerquote", stickerquote))
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"^-sms\s+\d{1,3}$"),
            sms_purge,
        )
    )

    logging.getLogger(__name__).info("🤖 Bot is running... Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
