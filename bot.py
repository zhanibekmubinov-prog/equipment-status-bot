# -*- coding: utf-8 -*-
"""Equipment status bot: step-by-step updates + daily summary + russian menu.

v6: бот пишет в платформу CIS (раздел «Техника → Статус техники»), а не в
Google-таблицу «Статус Техники». Диалог, кнопки и сводка — прежние; сменился
только источник данных: техника читается и обновление записывается через
API платформы (/api/v1/equipment-status).

v7: запись ТО / ремонта в реестр ТО платформы (кнопка «🔧 ТО / ремонт» в
меню и в карточке машины, команда /to). Короткий набор полей бланка F-002:
вид и номер ТО, дата, что сделано, показание счётчика, закрытие дефектного
акта. Полный бланк, правка и удаление записи — в платформе. API —
/api/v1/equipment-service, тот же ключ PLATFORM_KEY.

Переменные окружения (Railway -> Variables):
    BOT_TOKEN          - токен бота от BotFather (обязательно)
    PLATFORM_URL       - адрес платформы, например https://cis.example.kz
                         (обязательно)
    PLATFORM_KEY       - ключ бота: тот же, что в платформе
                         «Админ-панель → Интеграции → Ключ Telegram-бота
                         „Статус техники“» (обязательно)
    SUMMARY_CHAT_ID    - ID группового чата для сводки (например -1001234567890)
    SUMMARY_TIME       - время сводки, по умолчанию 18:00
    TIMEZONE           - по умолчанию Asia/Atyrau
    ALLOWED_USERS      - ID через запятую (пусто = разрешено всем)

GOOGLE_CREDS_JSON больше не нужен — его можно удалить из Variables.
Локальный запуск: токен можно положить в bot_token.txt рядом со скриптом.
"""
import datetime as dt
import html
import os
import re
from collections import Counter
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, Update)
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, MessageHandler,
                          filters)

# --- Настройки (env-переменные с запасными значениями) --------------------

PLATFORM_URL = os.getenv("PLATFORM_URL", "").strip().rstrip("/")
PLATFORM_KEY = os.getenv("PLATFORM_KEY", "").strip()
TOKEN_FILE = "bot_token.txt"

ALLOWED_USERS = [int(x) for x in
                 os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x]

SUMMARY_CHAT_ID = int(os.getenv("SUMMARY_CHAT_ID", "0") or "0")
SUMMARY_TIME = os.getenv("SUMMARY_TIME", "18:00")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Atyrau")

STATUS_LIST = ["В работе", "В резерве", "В ремонте", "Ожидание", "Новая"]
PROJECT_LIST = ["ЭМГ", "CIS", "КРС", "Простой"]

def now_local():
    """Текущее время в часовом поясе TIMEZONE (на хостинге сервер живёт в UTC)."""
    return datetime.now(ZoneInfo(TIMEZONE))


KEY = {"Статус": "status", "Проект": "project", "Текущая локация": "location",
       "Текущее состояние": "condition", "Последний ремонт": "repair",
       "Примечание": "note"}

# Поле в диалоге → поле в API платформы.
API_FIELD = {"Статус": "status", "Проект": "project",
             "Текущая локация": "location", "Текущее состояние": "condition",
             "Последний ремонт": "last_repair", "Примечание": "notes"}

(CATEGORY, UNIT, EDIT_MENU, FIELD_INPUT, SERVICE) = range(5)

# --- Меню ------------------------------------------------------------------

BTN_UPDATE = "📝 Обновить статус"
BTN_SUMMARY = "📋 Сводка"
BTN_SERVICE = "🔧 ТО / ремонт"

MENU_KB = ReplyKeyboardMarkup([[BTN_UPDATE, BTN_SERVICE], [BTN_SUMMARY]],
                              resize_keyboard=True, is_persistent=True)

# Кнопки нижнего меню — это обычный текст, поэтому их нужно исключать
# из всех остальных текстовых обработчиков, иначе «📋 Сводка», нажатая
# внутри диалога, запишется в платформу как значение поля.
NOT_MENU_BTN = (~filters.Regex(f"^{re.escape(BTN_UPDATE)}$")
                & ~filters.Regex(f"^{re.escape(BTN_SUMMARY)}$")
                & ~filters.Regex(f"^{re.escape(BTN_SERVICE)}$"))

# Любой текст в личном чате (кроме кнопок меню) считаем поисковым запросом.
SEARCH_FILTER = (filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE
                 & NOT_MENU_BTN)

# Текст, который принимается как новое значение поля: что угодно, кроме
# команд и кнопок меню.
FIELD_TEXT_FILTER = filters.TEXT & ~filters.COMMAND & NOT_MENU_BTN

BOT_COMMANDS = [
    BotCommand("start", "📝 Обновить статус техники"),
    BotCommand("to", "🔧 Записать ТО / ремонт"),
    BotCommand("summary", "📋 Сводка за день"),
    BotCommand("cancel", "❌ Отменить текущий ввод"),
    BotCommand("id", "Показать мой ID"),
    BotCommand("chatid", "Показать ID этого чата"),
]

# В группах показываем только нужное
GROUP_COMMANDS = [
    BotCommand("summary", "📋 Сводка за день"),
]


async def post_init(app: Application):
    from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
    # личные чаты — полный список
    await app.bot.set_my_commands(
        BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    # группы — только сводка
    await app.bot.set_my_commands(
        GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())


# --- Платформа CIS ---------------------------------------------------------

class PlatformError(Exception):
    """Платформа отказала: текст ошибки показываем человеку как есть."""


_http = None


def platform(method, path="", base="/api/v1/equipment-status", **kw):
    """Запрос к API платформы (статус техники или реестр ТО). Ответ —
    словарь из JSON платформы."""
    global _http
    if not PLATFORM_URL or not PLATFORM_KEY:
        raise PlatformError("не заданы PLATFORM_URL и PLATFORM_KEY")
    if _http is None:
        _http = httpx.Client(
            base_url=PLATFORM_URL, timeout=30,
            headers={"Authorization": f"Bearer {PLATFORM_KEY}"})
    r = _http.request(method, base + path, **kw)
    try:
        data = r.json()
    except ValueError:
        raise PlatformError(f"платформа ответила {r.status_code}")
    if r.status_code != 200 or not data.get("ok"):
        raise PlatformError(data.get("error")
                            or f"платформа ответила {r.status_code}")
    return data


def load_status():
    """Вся техника + коды, обновлённые сегодня (для сводки)."""
    return platform("GET")


def load_units():
    """Техника с теми же ключами, что раньше брались из листа «База»:
    code, category, brand, gos, location, project, status, condition,
    repair, note."""
    return load_status()["units"]


def apply_changes(unit, changes, who, tg_id):
    """Записывает в платформу только изменённые поля. Платформа кладёт их на
    ТЕКУЩИЕ значения машины и сама добавляет строку журнала, дату и автора."""
    platform("POST", "/" + quote(unit["code"], safe=""),
             json={"changes": {API_FIELD[f]: v for f, v in changes.items()},
                   "tg_id": tg_id, "author": who})


SERVICE_API = "/api/v1/equipment-service"


def load_service_form(code):
    """Что предложить в диалоге ТО: виды ТО по нормативам машины, счётчики
    с последним показанием, открытые дефектные акты."""
    return platform("GET", "/" + quote(code, safe=""), base=SERVICE_API)


def add_service_record(code, record, who, tg_id):
    """Записывает ТО / ремонт в реестр ТО платформы. Проверки — те же, что у
    формы F-002 в платформе: ошибку платформа вернёт текстом."""
    return platform("POST", "/" + quote(code, safe=""), base=SERVICE_API,
                    json=dict(record, tg_id=tg_id, author=who))


# --- Сводка ---------------------------------------------------------------

def unit_sentence(u):
    def e(s):
        return html.escape(s)

    text = f"<b>{e(u['brand'][:45])}</b> ({u['code']})"

    cond = u["condition"]
    if cond and cond.lower() != "без дефектов":
        text += f" — {e(cond[:80])}."
    else:
        text += " — без дефектов."

    if u["location"]:
        place = f" Находится: {e(u['location'])}"
        if u["project"]:
            place += f" ({e(u['project'])})"
        text += place + "."

    text += f" Статус: {u['status'].lower()}."

    if u["note"]:
        text += f" {e(u['note'][:100])}."

    return text


def build_summary():
    data = load_status()
    units = data["units"]
    today = now_local().strftime("%d.%m.%Y")

    by_code = {u["code"]: u for u in units}
    st = Counter(u["status"] for u in units)
    idle = sum(1 for u in units if u["project"] == "Простой")

    lines = [f"📋 <b>Сводка по технике за {today}</b>", "",
             f"🟢 В работе: {st.get('В работе', 0)}   "
             f"🟡 В резерве: {st.get('В резерве', 0)}   "
             f"🔴 В ремонте: {st.get('В ремонте', 0)}   "
             f"⏸ Простой: {idle}"]

    # коды техники, обновлённой за сегодня, — считает платформа по журналу
    changed_codes = data["today"]

    if changed_codes:
        by_cat = {}
        for code in changed_codes:
            u = by_code.get(code)
            if u:
                by_cat.setdefault(u["category"], []).append(u)
        lines.append("\n<b>Изменения за день:</b>")
        for cat, items in by_cat.items():
            lines.append(f"\n<u>{html.escape(cat)}</u>")
            for u in items:
                lines.append("▸ " + unit_sentence(u))
    else:
        lines.append("\nЗа сегодня изменений не было.")

    attention = [u for u in units if u["status"] in ("В ремонте", "Ожидание")]
    if attention:
        lines.append("\n⚠️ <b>Требуют внимания:</b>")
        for u in attention:
            lines.append("▸ " + unit_sentence(u))

    return "\n".join(lines)


# Telegram hard limit is 4096 characters per message. We keep a margin so
# that re-opened tags and the "1/3" page marker always fit.
TG_MSG_LIMIT = 4096
CHUNK_LIMIT = 3800
PAGE_MARK_ROOM = 32     # room reserved for the "\n\n<i>1/3</i>" footer

_TAG_RE = re.compile(r"<(/?)(\w+)([^>]*)>")


def split_html(text, limit=CHUNK_LIMIT):
    """Split HTML text into chunks that Telegram will accept.

    Guarantees for every returned chunk:
      * len(chunk) <= limit  (well under Telegram's 4096 limit);
      * every tag opened in the chunk is also closed in it — if a cut
        happens inside <b>...</b>, the tag is closed before the cut and
        re-opened at the start of the next chunk;
      * cuts never land inside a tag (<b) or an HTML entity (&amp;);
      * cuts prefer a line break, then a space, so words stay intact.

    Concatenating the chunks (minus the injected closing/opening tags)
    reproduces the original text.
    """
    chunks = []
    open_tags = []          # stack of full opening tags, e.g. ['<b>', '<u>']
    buf = ""

    def closing_for(stack):
        return "".join("</%s>" % _TAG_RE.match(t).group(2)
                       for t in reversed(stack))

    def flush():
        """Close the current chunk and start a new one with the same tags."""
        nonlocal buf
        if _TAG_RE.sub("", buf).strip():
            chunks.append(buf + closing_for(open_tags))
            buf = "".join(open_tags)
        else:
            # Nothing printable yet: Telegram rejects blank messages, so
            # carry the whitespace over instead of emitting an empty chunk.
            carried = _TAG_RE.sub("", buf)
            if len(carried) > limit // 2:
                # Pathological padding: collapse it, but keep one separator
                # so that words on either side never get glued together.
                carried = "\n" if "\n" in carried else " "
            buf = "".join(open_tags) + carried

    def find_cut(s, room):
        """Best cut position in plain text s, at most `room` chars."""
        window = s[:room]
        for sep in ("\n", " "):
            pos = window.rfind(sep)
            if pos > 0:
                return pos + 1
        # No separator: cut hard, but not inside an HTML entity.
        amp = window.rfind("&")
        if amp > 0 and ";" not in window[amp:]:
            return amp
        return room

    pos = 0
    for m in _TAG_RE.finditer(text):
        # --- plain text before this tag -------------------------------
        chunk_text = text[pos:m.start()]
        while chunk_text:
            room = limit - len(buf) - len(closing_for(open_tags))
            if room <= 0:
                flush()
                continue
            if len(chunk_text) <= room:
                buf += chunk_text
                break
            cut = find_cut(chunk_text, room)
            buf += chunk_text[:cut]
            chunk_text = chunk_text[cut:]
            flush()

        # --- the tag itself: never split, keep the pair together ------
        tag = m.group(0)
        is_close = bool(m.group(1))
        self_closing = m.group(3).rstrip().endswith("/")
        if is_close:
            # Closing a tag shrinks the reserved space, so it always fits.
            buf += tag
            if open_tags:
                open_tags.pop()
        else:
            # An opening tag also reserves room for its own closing tag.
            extra = 0 if self_closing else len("</%s>" % m.group(2))
            if len(buf) + len(tag) + len(closing_for(open_tags)) + extra > limit:
                flush()
            buf += tag
            if not self_closing:
                open_tags.append(tag)
        pos = m.end()

    # --- trailing text -------------------------------------------------
    tail = text[pos:]
    while tail:
        room = limit - len(buf) - len(closing_for(open_tags))
        if room <= 0:
            flush()
            continue
        if len(tail) <= room:
            buf += tail
            break
        cut = find_cut(tail, room)
        buf += tail[:cut]
        tail = tail[cut:]
        flush()

    if _TAG_RE.sub("", buf).strip():
        chunks.append(buf + closing_for(open_tags))
    return chunks


async def send_summary_to(bot, chat_id):
    """Send the daily summary, split across numbered messages if needed."""
    text = build_summary()
    chunks = split_html(text, CHUNK_LIMIT - PAGE_MARK_ROOM)
    total = len(chunks)

    for num, chunk in enumerate(chunks, 1):
        # A single message needs no page marker.
        if total > 1:
            chunk = f"{chunk}\n\n<i>{num}/{total}</i>"
        try:
            await bot.send_message(chat_id, chunk, parse_mode="HTML")
        except BadRequest as err:
            # Last-resort fallback: if Telegram still rejects the markup,
            # send the text without formatting so the summary is not lost.
            if "parse entities" not in str(err).lower():
                raise
            plain = html.unescape(_TAG_RE.sub("", chunk))
            await bot.send_message(chat_id, plain[:TG_MSG_LIMIT])


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ALLOWED_USERS and user.id not in ALLOWED_USERS:
        return
    try:
        await send_summary_to(context.bot, update.effective_chat.id)
    except Exception as e:
        await update.message.reply_text(f"Ошибка сводки: {e}")


async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    if SUMMARY_CHAT_ID:
        try:
            await send_summary_to(context.bot, SUMMARY_CHAT_ID)
        except Exception as e:
            print(f"Ошибка отправки сводки: {e}")


async def chat_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ID этого чата: {update.effective_chat.id}")


# --- Вспомогательные ------------------------------------------------------

def kb(buttons, cols=2):
    rows = [buttons[i:i + cols] for i in range(0, len(buttons), cols)]
    return InlineKeyboardMarkup(rows)


def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def cancel_row():
    return btn("❌ Отмена", "cancel")


def unit_card(u):
    def esc(s):
        return html.escape(s) if s else "—"
    return (f"<b>{esc(u['code'])} — {esc(u['brand'])}</b>\n"
            f"Категория: {esc(u['category'])}\n"
            f"Гос. номер: {esc(u['gos'])}\n"
            f"Текущая локация: {esc(u['location'])}\n"
            f"Проект: {esc(u['project'])}\n"
            f"Статус: {esc(u['status'])}\n"
            f"Состояние: {esc(u['condition'])}\n"
            f"Последний ремонт: {esc(u['repair'])}\n"
            f"Примечание: {esc(u['note'])}")


def who_is(update):
    u = update.effective_user
    name = u.full_name or ""
    if u.username:
        name += f" (@{u.username})"
    return name.strip() or str(u.id)


async def stale_session(update):
    """Контекст диалога потерян (бот перезапускался, а кнопки остались)."""
    await send(update, "⚠️ Сессия устарела — бот перезапускался.\n"
                       "Нажмите «📝 Обновить статус» или напишите код техники, "
                       "чтобы начать заново.")
    return ConversationHandler.END


def has_state(context, *keys):
    return all(k in context.user_data for k in keys)


async def send(update, text, markup=None):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


# --- Диалог обновления ----------------------------------------------------

# Поля, которые можно менять: (название поля, подпись на кнопке).
FIELDS = [
    ("Статус", "🔴 Статус"),
    ("Проект", "🏗 Проект"),
    ("Текущая локация", "📍 Локация"),
    ("Текущее состояние", "🔧 Состояние"),
    ("Последний ремонт", "🛠 Ремонт"),
    ("Примечание", "📝 Примечание"),
]

# Поля, которые логично очищать одной кнопкой.
CLEARABLE = {"Примечание", "Текущее состояние"}

# Поля, где текст накапливается: старая запись остаётся актуальной, а новая
# добавляется к ней («ожидает форсунки» + «повреждены колёса»).
APPENDABLE = {"Текущее состояние", "Примечание"}
APPEND_SEP = "; "

MAX_SEARCH_RESULTS = 30


def field_options(field):
    """Готовые значения для поля: нажатие кнопки = сразу выбор."""
    if field == "Статус":
        return STATUS_LIST
    if field == "Проект":
        return PROJECT_LIST
    if field == "Текущее состояние":
        return ["Без дефектов"]
    if field == "Последний ремонт":
        return [now_local().strftime("%d.%m.%Y")]
    return []


def edit_card(unit, changes):
    """Карточка техники: изменённые поля показаны как «старое → новое»."""
    def esc(s):
        return html.escape(s) if s else "—"

    lines = [f"<b>{esc(unit['code'])} — {esc(unit['brand'])}</b>",
             f"{esc(unit['category'])} · гос. номер {esc(unit['gos'])}", ""]
    for field, _ in FIELDS:
        old = unit[KEY[field]]
        if field in changes:
            lines.append(f"▸ <b>{field}</b>: <s>{esc(old)}</s> → "
                         f"<b>{esc(changes[field])}</b>")
        else:
            lines.append(f"{field}: {esc(old)}")
    lines.append("")
    if changes:
        lines.append(f"Правок: {len(changes)}. Можно изменить ещё поле — "
                     f"или нажать большую кнопку "
                     f"<b>«✅ ЗАПИСАТЬ»</b> в самом низу 👇")
    else:
        lines.append("Нажмите поле, которое изменилось — "
                     "остальные останутся как есть.")
    return "\n".join(lines)


def save_button(changes):
    """Широкая кнопка записи — всегда одна в своём ряду, в самом низу."""
    if changes:
        return btn(f"✅ ЗАПИСАТЬ ({len(changes)}) ✅", "save")
    return btn("✅ ЗАПИСАТЬ", "save")


def edit_menu_kb(changes):
    buttons = [btn(f"{label}{' ✏️' if field in changes else ''}", f"f:{i}")
               for i, (field, label) in enumerate(FIELDS)]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    if changes:
        rows.append([btn("↩️ Сбросить правки", "reset")])
    else:
        rows.append([btn("🔧 Записать ТО / ремонт", "s:start")])
    rows.append([btn("⬅️ К списку", "back"), cancel_row()])
    # Последний ряд — только запись: она занимает всю ширину экрана и
    # отделена от служебных кнопок, поэтому её не пропускают.
    rows.append([save_button(changes)])
    return InlineKeyboardMarkup(rows)


async def show_edit_menu(update, context):
    if not has_state(context, "unit", "changes"):
        return await stale_session(update)
    unit = context.user_data["unit"]
    changes = context.user_data["changes"]
    await send(update, edit_card(unit, changes), edit_menu_kb(changes))
    return EDIT_MENU


# --- Поиск техники --------------------------------------------------------

def search_units(units, query):
    """Поиск по коду, марке и гос. номеру. None = слишком короткий запрос."""
    q = " ".join(query.lower().split())
    if len(q) < 2:
        return None
    return [u for u in units
            if q in u["code"].lower()
            or q in u["brand"].lower()
            or q in u["gos"].lower()]


async def show_search_results(update, context, query):
    units = context.user_data.get("units") or []
    hits = search_units(units, query)

    if hits is None:
        await send(update, "Введите минимум 2 символа — код, марку "
                           "или гос. номер.")
        return UNIT

    if not hits:
        await send(update, f"По запросу «{html.escape(query)}» ничего не найдено.\n"
                           "Попробуйте код (EX-014), марку (komatsu) "
                           "или гос. номер.",
                   kb([btn("⬅️ К категориям", "back"), cancel_row()], 2))
        return UNIT

    if len(hits) == 1:
        return await open_unit(update, context, hits[0])

    if len(hits) > MAX_SEARCH_RESULTS:
        await send(update, f"Найдено {len(hits)} — слишком много. "
                           "Уточните запрос.",
                   kb([btn("⬅️ К категориям", "back"), cancel_row()], 2))
        return UNIT

    buttons = [btn(f"{u['code']} · {u['brand'][:28]}", f"unit:{u['code']}")
               for u in hits]
    buttons += [btn("⬅️ К категориям", "back"), cancel_row()]
    await send(update, f"Найдено {len(hits)} — выберите технику:",
               kb(buttons, 1))
    return UNIT


async def search_in_dialog(update, context):
    """Поиск текстом внутри диалога (на экране категорий или списка)."""
    return await show_search_results(update, context, update.message.text)


async def search_entry(update, context):
    """Поиск текстом без входа в меню: пользователь просто пишет код."""
    user = update.effective_user
    if ALLOWED_USERS and user.id not in ALLOWED_USERS:
        await update.message.reply_text(
            f"Доступ закрыт. Ваш ID: {user.id} — передайте администратору.")
        return ConversationHandler.END

    try:
        units = load_units()
    except Exception as e:
        await update.message.reply_text(f"Ошибка связи с платформой: {e}")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["units"] = units
    context.user_data["cats"] = list(dict.fromkeys(u["category"] for u in units))
    return await show_search_results(update, context, update.message.text)


# --- Выбор техники --------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await begin(update, context, "status")


async def start_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """«🔧 ТО / ремонт»: тот же выбор машины, затем запись ТО."""
    return await begin(update, context, "service")


async def begin(update, context, mode):
    user = update.effective_user
    if ALLOWED_USERS and user.id not in ALLOWED_USERS:
        await update.message.reply_text(
            f"Доступ закрыт. Ваш ID: {user.id} — передайте администратору.")
        return ConversationHandler.END

    if (update.message and update.effective_chat.type == "private"
            and not context.chat_data.get("menu_shown")):
        await update.message.reply_text(
            "Кнопки меню включены — они внизу экрана. 👇", reply_markup=MENU_KB)
        context.chat_data["menu_shown"] = True

    context.user_data.clear()
    try:
        units = load_units()
    except Exception as e:
        await update.message.reply_text(f"Ошибка связи с платформой: {e}")
        return ConversationHandler.END

    context.user_data["units"] = units
    context.user_data["mode"] = mode
    cats = list(dict.fromkeys(u["category"] for u in units))
    context.user_data["cats"] = cats

    buttons = [btn(c, f"cat:{i}") for i, c in enumerate(cats)] + [cancel_row()]
    title = ("🔧 <b>Запись ТО / ремонта.</b>\n" if mode == "service"
             else "")
    await send(update, title + "Выберите категорию — или просто напишите код, марку "
                       "либо гос. номер техники (например <code>EX-014</code> "
                       "или <code>komatsu</code>):", kb(buttons, 2))
    return CATEGORY


async def pick_category(update, context):
    if not has_state(context, "cats", "units"):
        return await stale_session(update)
    idx = int(update.callback_query.data.split(":")[1])
    if idx >= len(context.user_data["cats"]):
        return await stale_session(update)
    cat = context.user_data["cats"][idx]
    units = [u for u in context.user_data["units"] if u["category"] == cat]
    buttons = [btn(f"{u['code']} · {u['brand'][:28]}", f"unit:{u['code']}")
               for u in units]
    buttons += [btn("⬅️ Назад", "back"), cancel_row()]
    await send(update, f"<b>{html.escape(cat)}</b> — выберите технику "
                       "(или напишите код/марку для поиска):", kb(buttons, 1))
    return UNIT


async def back_to_categories(update, context):
    if not has_state(context, "cats"):
        return await stale_session(update)
    cats = context.user_data["cats"]
    buttons = [btn(c, f"cat:{i}") for i, c in enumerate(cats)] + [cancel_row()]
    await send(update, "Выберите категорию — или напишите код/марку техники:",
               kb(buttons, 2))
    return CATEGORY


async def open_unit(update, context, unit):
    context.user_data["unit"] = unit
    context.user_data["changes"] = {}
    context.user_data.pop("append", None)
    if context.user_data.get("mode") == "service":
        return await service_start(update, context)
    return await show_edit_menu(update, context)


async def pick_unit(update, context):
    if not has_state(context, "units"):
        return await stale_session(update)
    code = update.callback_query.data.split(":")[1]
    unit = next((u for u in context.user_data["units"] if u["code"] == code),
                None)
    if unit is None:
        return await stale_session(update)
    return await open_unit(update, context, unit)


# --- Изменение одного поля ------------------------------------------------

async def pick_field(update, context):
    if not has_state(context, "unit", "changes"):
        return await stale_session(update)
    idx = int(update.callback_query.data.split(":")[1])
    field = FIELDS[idx][0]
    context.user_data["field"] = field
    context.user_data["append"] = False

    unit = context.user_data["unit"]
    current = context.user_data["changes"].get(field, unit[KEY[field]])
    options = field_options(field)

    opts = [btn(v, f"v:{i}") for i, v in enumerate(options)]
    rows = [opts[i:i + 2] for i in range(0, len(opts), 2)]

    can_append = field in APPENDABLE and bool((current or "").strip())
    if can_append:
        # Отдельным широким рядом, чтобы «дописать» не путали с заменой.
        rows.append([btn("➕ ДОПИСАТЬ к текущему", "append")])

    last = []
    if field in CLEARABLE:
        last.append(btn("🧹 Очистить", "clear"))
    last += [btn("⬅️ Назад", "back"), cancel_row()]
    rows.append(last)

    hint = ("Выберите значение или напишите текстом:" if options
            else "Напишите новое значение текстом:")
    if can_append:
        hint += ("\n\n⚠️ Новый текст <b>заменит</b> старый. "
                 "Если старая запись ещё актуальна — нажмите "
                 "«➕ ДОПИСАТЬ к текущему».")
    await send(update,
               f"<b>{field}</b>\nСейчас: {html.escape(current) if current else '—'}\n\n{hint}",
               InlineKeyboardMarkup(rows))
    return FIELD_INPUT


async def append_prompt(update, context):
    """Экран «дописать»: следующий текст добавится к тому, что уже есть."""
    if not has_state(context, "field", "unit", "changes"):
        return await stale_session(update)
    field = context.user_data["field"]
    unit = context.user_data["unit"]
    current = context.user_data["changes"].get(field, unit[KEY[field]]) or ""
    context.user_data["append"] = True
    await send(update,
               f"<b>{field}</b> — дописать\n"
               f"Останется: {html.escape(current)}\n\n"
               "Напишите, что добавить (например «повреждены колёса») — "
               "старая запись сохранится, новая добавится через «;».",
               kb([btn("⬅️ Назад", "back"), cancel_row()], 2))
    return FIELD_INPUT


async def store_value(update, context, value):
    """Запоминает правку (и не считает правкой то, что не изменилось)."""
    if not has_state(context, "field", "unit", "changes"):
        return await stale_session(update)
    field = context.user_data["field"]
    unit = context.user_data["unit"]
    value = value.strip()
    if context.user_data.pop("append", False):
        current = (context.user_data["changes"].get(field, unit[KEY[field]])
                   or "").strip().rstrip(";,.").strip()
        if not value:
            # Дописать нечего — оставляем как было, поле не затираем.
            return await show_edit_menu(update, context)
        if current:
            value = f"{current}{APPEND_SEP}{value}"
    if value == (unit[KEY[field]] or "").strip():
        context.user_data["changes"].pop(field, None)
    else:
        context.user_data["changes"][field] = value
    return await show_edit_menu(update, context)


async def set_option(update, context):
    idx = int(update.callback_query.data.split(":")[1])
    field = context.user_data["field"]
    return await store_value(update, context, field_options(field)[idx])


async def set_text_value(update, context):
    return await store_value(update, context, update.message.text)


async def clear_field(update, context):
    return await store_value(update, context, "")


async def field_back(update, context):
    context.user_data.pop("append", None)
    return await show_edit_menu(update, context)


async def reset_changes(update, context):
    if not has_state(context, "unit"):
        return await stale_session(update)
    context.user_data["changes"] = {}
    return await show_edit_menu(update, context)


# --- Запись ---------------------------------------------------------------

async def save_changes(update, context):
    if not has_state(context, "unit", "changes"):
        return await stale_session(update)
    unit = context.user_data["unit"]
    changes = context.user_data["changes"]
    if not changes:
        await update.callback_query.answer("Изменений нет", show_alert=True)
        return EDIT_MENU

    try:
        apply_changes(unit, changes, who_is(update), update.effective_user.id)
    except Exception as e:
        await send(update, f"⚠️ Ошибка записи в платформу: {e}\n"
                           "Изменения НЕ сохранены. Попробуйте ещё раз.")
        return ConversationHandler.END

    lines = [f"✅ Записано: <b>{html.escape(unit['code'])} — "
             f"{html.escape(unit['brand'])}</b>", ""]
    for field, val in changes.items():
        lines.append(f"▸ {field}: <b>{html.escape(val) if val else '—'}</b>")
    lines.append("\nЧтобы обновить ещё технику — напишите её код "
                 "или нажмите «📝 Обновить статус».")
    await send(update, "\n".join(lines))
    return ConversationHandler.END


async def cancel(update, context):
    await send(update, "Отменено. Нажмите «📝 Обновить статус» или напишите "
                       "код техники, чтобы начать заново.")
    return ConversationHandler.END


async def my_id(update, context):
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def unknown_text(update, context):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "Я жду нажатия кнопки. Если запутались — /cancel, затем "
            "«📝 Обновить статус».")


# --- Запись ТО / ремонта (v7) ---------------------------------------------
# Шаги идут по порядку, пока чего-то не хватает: вид → номер ТО → дата →
# что сделано → показание счётчика → закрытие дефекта (только ремонт) →
# проверка и запись. Всё, что ввели, — в context.user_data["svc"];
# «✏️ Заново» очищает ввод и проходит шаги снова.

DESCRIPTION_MAX = 2000


def fmt_num(x):
    return str(int(x)) if float(x).is_integer() else str(round(x, 1))


def parse_number(text):
    """«12 481,5» → 12481.5; не число → None."""
    t = (text or "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(t) if t else None
    except ValueError:
        return None


def parse_day(text, today):
    """«28.10.2026», «28.10.26», «28.10» → дата; иначе None."""
    m = re.fullmatch(r"\s*(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2}|\d{4}))?\s*",
                     text or "")
    if not m:
        return None
    day, month, year = int(m[1]), int(m[2]), m[3]
    if not year:
        year = today.year
    else:
        year = int(year) + (2000 if len(year) == 2 else 0)
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def service_meter(svc):
    """Счётчик записи: выбранный, основной по нормативу или единственный."""
    form = svc["form"]
    if svc.get("meter"):
        return svc["meter"]
    if form.get("meter"):
        return form["meter"]
    if len(form.get("meters") or []) == 1:
        return form["meters"][0]["code"]
    return None


def meter_info(form, code):
    for m in (form.get("meters") or []) + (form.get("meter_choices") or []):
        if m["code"] == code:
            return m
    return {"code": code, "title": code, "unit": "", "value": None}


def service_card(unit, svc):
    """Что уже введено — над каждым шагом и на экране проверки."""
    def esc(s):
        return html.escape(str(s)) if s not in (None, "") else "—"
    form = svc["form"]
    lines = [f"🔧 <b>{esc(unit['code'])} — {esc(unit['brand'])}</b>"]
    if svc.get("kind"):
        what = (svc.get("code") or "ТО") if svc["kind"] == "ТО" else "Ремонт"
        lines.append(f"Вид: <b>{esc(what)}</b>")
    if svc.get("date"):
        lines.append(f"Дата: <b>{svc['date']:%d.%m.%Y}</b>")
    if svc.get("description"):
        lines.append(f"Что сделано: {esc(svc['description'])}")
    if svc.get("reading") is not None:
        m = meter_info(form, service_meter(svc))
        lines.append(f"Показание: <b>{fmt_num(svc['reading'])} {esc(m['unit'])}"
                     f"</b> ({esc(m['title'])})")
    elif svc.get("reading_skipped"):
        lines.append("Показание: —")
    defect = next((d for d in form.get("defects") or []
                   if d["id"] == svc.get("defect_id")), None)
    if defect:
        lines.append(f"Закрывает дефект: <b>{esc(defect['title'])}</b>")
    return "\n".join(lines)


def service_record(svc):
    """Тело запроса к платформе — поля как у формы F-002."""
    record = {"kind": svc["kind"], "date": svc["date"].isoformat(),
              "description": svc["description"]}
    if svc["kind"] == "ТО":
        record["norm_code"] = svc.get("code") or "ТО"
    if svc.get("reading") is not None:
        record["reading"] = svc["reading"]
        record["meter"] = service_meter(svc)
    if svc.get("defect_id"):
        record["defect_id"] = svc["defect_id"]
        record["closes_defect"] = True
    return record


def service_rows(*rows):
    """Кнопки шага + нижний ряд «к карточке / отмена»."""
    return InlineKeyboardMarkup([list(r) for r in rows if r]
                                + [[btn("⬅️ К карточке", "s:card"),
                                    cancel_row()]])


async def service_start(update, context):
    """Начало записи ТО по выбранной машине."""
    unit = context.user_data.get("unit")
    if unit is None:
        return await stale_session(update)
    try:
        form = load_service_form(unit["code"])
    except Exception as e:
        await send(update, f"Ошибка связи с платформой: {html.escape(str(e))}")
        return ConversationHandler.END
    if not form.get("platform"):
        await send(update, "Реестр ТО ещё ведётся в Google-таблице — записывать "
                           "ТО из бота можно после его переноса в платформу.")
        return ConversationHandler.END
    context.user_data["svc"] = {"form": form}
    return await service_step(update, context)


async def service_step(update, context, note=""):
    """Показывает первый шаг, которого ещё не хватает."""
    if not has_state(context, "unit", "svc"):
        return await stale_session(update)
    unit, svc = context.user_data["unit"], context.user_data["svc"]
    form = svc["form"]
    svc["await"] = None
    head = (note + "\n\n" if note else "") + service_card(unit, svc) + "\n\n"

    if not svc.get("kind"):
        await send(update, head + "Что делали?", service_rows(
            [btn("🛠 ТО", "s:k:to"), btn("🔧 Ремонт", "s:k:r")]))
        return SERVICE

    codes = form.get("codes") or ["ТО"]
    if svc["kind"] == "ТО" and not svc.get("code"):
        if len(codes) == 1:
            svc["code"] = codes[0]
        else:
            buttons = [btn(c, f"s:c:{i}") for i, c in enumerate(codes)]
            await send(update, head + "Какое ТО? «ТО» без номера — если "
                                      "номер не важен.",
                       service_rows(buttons))
            return SERVICE

    if not svc.get("date"):
        today = now_local().date()
        days = [(0, "Сегодня"), (1, "Вчера"), (2, "Позавчера")]
        buttons = [btn(f"{t} {today - dt.timedelta(days=n):%d.%m}", f"s:d:{n}")
                   for n, t in days]
        svc["await"] = "date"
        await send(update, head + "Когда? Нажмите кнопку или напишите дату "
                                  "(например <code>25.10</code>).",
                   service_rows(buttons))
        return SERVICE

    if not svc.get("description"):
        svc["await"] = "description"
        await send(update, head + "Что сделано? Напишите текстом — например "
                                  "<i>замена масла ДВС, фильтры масляный и "
                                  "топливный</i>.", service_rows())
        return SERVICE

    if svc.get("reading") is None and not svc.get("reading_skipped"):
        meter = service_meter(svc)
        if meter is None:
            buttons = [btn(f"{m['title']}, {m['unit']}", f"s:m:{i}")
                       for i, m in enumerate(form.get("meter_choices") or [])]
            await send(update, head + "Показание какого счётчика записать?",
                       service_rows(buttons[:2], buttons[2:],
                                    [btn("⏭ Без показания", "s:skip")]))
            return SERVICE
        m = meter_info(form, meter)
        last = ""
        if m.get("value") is not None:
            last = (f"\nПоследнее: {fmt_num(m['value'])} "
                    f"{html.escape(m['unit'])} на "
                    f"{dt.date.fromisoformat(m['date']):%d.%m.%Y}.")
        svc["await"] = "reading"
        await send(update, head + f"Показание счётчика "
                                  f"«{html.escape(m['title'])}», "
                                  f"{html.escape(m['unit'])}?{last}\n"
                                  "Напишите число — или нажмите "
                                  "«Без показания».",
                   service_rows([btn("⏭ Без показания", "s:skip")]))
        return SERVICE

    defects = form.get("defects") or []
    if svc["kind"] == "Р" and defects and "defect_id" not in svc:
        buttons = [[btn(f"✅ {d['title'][:48]}", f"s:x:{i}")]
                   for i, d in enumerate(defects)]
        await send(update, head + "Ремонт устранил дефект по акту? Акт "
                                  "закроется вместе с записью.",
                   service_rows(*buttons, [btn("Нет, акт не закрывать",
                                               "s:x:no")]))
        return SERVICE

    await send(update, head + "Проверьте и нажмите <b>«✅ ЗАПИСАТЬ»</b>.",
               InlineKeyboardMarkup([
                   [btn("✏️ Заново", "s:redo"), cancel_row()],
                   [btn("✅ ЗАПИСАТЬ В РЕЕСТР ТО ✅", "s:save")]]))
    return SERVICE


async def service_button(update, context):
    if not has_state(context, "unit", "svc"):
        return await stale_session(update)
    svc = context.user_data["svc"]
    form = svc["form"]
    parts = update.callback_query.data.split(":")
    action, arg = parts[1], (parts[2] if len(parts) > 2 else "")
    try:
        if action == "k":
            svc["kind"] = "ТО" if arg == "to" else "Р"
        elif action == "c":
            svc["code"] = (form.get("codes") or ["ТО"])[int(arg)]
        elif action == "d":
            svc["date"] = now_local().date() - dt.timedelta(days=int(arg))
        elif action == "m":
            svc["meter"] = form["meter_choices"][int(arg)]["code"]
        elif action == "skip":
            svc["reading"], svc["reading_skipped"] = None, True
        elif action == "x":
            svc["defect_id"] = (None if arg == "no"
                                else form["defects"][int(arg)]["id"])
        elif action == "redo":
            context.user_data["svc"] = {"form": form}
        elif action == "card":
            context.user_data.pop("svc", None)
            return await show_edit_menu(update, context)
        elif action == "save":
            return await service_save(update, context)
    except (IndexError, KeyError, ValueError):
        return await stale_session(update)
    return await service_step(update, context)


async def service_text(update, context):
    """Текст на шаге ТО: дата, что сделано или показание счётчика."""
    if not has_state(context, "unit", "svc"):
        return await stale_session(update)
    svc = context.user_data["svc"]
    text = (update.message.text or "").strip()
    waiting = svc.get("await")
    if waiting == "date":
        today = now_local().date()
        day = parse_day(text, today)
        if day is None:
            return await service_step(update, context,
                                      "⚠️ Не понял дату. Пример: 25.10.2026")
        if day > today:
            return await service_step(update, context,
                                      "⚠️ Дата в будущем — так нельзя.")
        svc["date"] = day
    elif waiting == "description":
        if not text:
            return await service_step(update, context)
        svc["description"] = text[:DESCRIPTION_MAX]
    elif waiting == "reading":
        value = parse_number(text)
        if value is None or value < 0:
            return await service_step(update, context,
                                      "⚠️ Показание — число, например 12480.")
        svc["reading"] = value
    else:
        await update.message.reply_text(
            "Нажмите кнопку под сообщением выше — или /cancel.")
        return SERVICE
    return await service_step(update, context)


async def service_save(update, context):
    unit, svc = context.user_data["unit"], context.user_data["svc"]
    try:
        res = add_service_record(unit["code"], service_record(svc),
                                 who_is(update), update.effective_user.id)
    except Exception as e:
        # платформа объясняет, что не так (показание меньше прошлого и т.п.)
        await send(update, f"⚠️ Платформа не приняла запись: "
                           f"{html.escape(str(e))}\n\n"
                           + service_card(unit, svc),
                   InlineKeyboardMarkup([
                       [btn("✏️ Заново", "s:redo"), cancel_row()],
                       [btn("🔁 Повторить запись", "s:save")]]))
        return SERVICE
    lines = [f"✅ Записано в реестр ТО: <b>{html.escape(unit['code'])} — "
             f"{html.escape(res['record']['label'])}</b>"]
    if res.get("closed"):
        lines.append(f"Дефектный акт {html.escape(res['closed'])} закрыт.")
    state = res.get("to_state")
    if state and state.get("remaining") is not None:
        left = f"{fmt_num(abs(state['remaining']))} {state['unit']}"
        if state["status"] == "due":
            lines.append(f"⚠️ {state['code']} просрочено на {left}.")
        else:
            lines.append(f"До {state['code']}: {left}.")
    lines.append("\nЕщё запись — «🔧 ТО / ремонт». Исправить запись можно "
                 "в платформе: Техника → машина → Реестр ТО.")
    context.user_data.pop("svc", None)
    await send(update, "\n".join(lines))
    return ConversationHandler.END


# --- Запуск ---------------------------------------------------------------

def get_token():
    t = os.getenv("BOT_TOKEN")
    if t:
        return t.strip()
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    raise SystemExit("Токен не найден: задайте переменную BOT_TOKEN "
                     "или создайте bot_token.txt")


def build_conversation():
    """Собирает диалог обновления.

    ВАЖНО: allow_reentry здесь НЕ включаем. В python-telegram-bot при
    allow_reentry=True список entry_points проверяется РАНЬШЕ обработчиков
    текущего состояния (ConversationHandler.check_update:
    `if state is None or self.allow_reentry`). Поскольку среди entry_points
    есть MessageHandler(SEARCH_FILTER, search_entry), который ловит любой
    текст в личном чате, любое текстовое сообщение внутри диалога
    перехватывалось поиском — и шаг «напишите значение текстом» никогда не
    срабатывал. Перезапуск диалога вместо этого обеспечивают fallbacks.
    """
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_UPDATE)}$"), start),
            CommandHandler("to", start_service),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_SERVICE)}$"),
                           start_service),
            # Написал код техники — сразу попал в её карточку.
            MessageHandler(SEARCH_FILTER, search_entry),
        ],
        states={
            CATEGORY: [CallbackQueryHandler(pick_category, pattern=r"^cat:"),
                       MessageHandler(SEARCH_FILTER, search_in_dialog)],
            UNIT: [CallbackQueryHandler(pick_unit, pattern=r"^unit:"),
                   CallbackQueryHandler(back_to_categories, pattern=r"^back$"),
                   MessageHandler(SEARCH_FILTER, search_in_dialog)],
            EDIT_MENU: [CallbackQueryHandler(pick_field, pattern=r"^f:\d+$"),
                        CallbackQueryHandler(save_changes, pattern=r"^save$"),
                        CallbackQueryHandler(reset_changes, pattern=r"^reset$"),
                        CallbackQueryHandler(back_to_categories, pattern=r"^back$"),
                        CallbackQueryHandler(service_start, pattern=r"^s:start$"),
                        # Написал код другой техники — переключились на неё.
                        MessageHandler(SEARCH_FILTER, search_in_dialog)],
            FIELD_INPUT: [CallbackQueryHandler(set_option, pattern=r"^v:\d+$"),
                          CallbackQueryHandler(append_prompt, pattern=r"^append$"),
                          CallbackQueryHandler(clear_field, pattern=r"^clear$"),
                          CallbackQueryHandler(field_back, pattern=r"^back$"),
                          MessageHandler(FIELD_TEXT_FILTER, set_text_value)],
            # Запись ТО: кнопки шагов и текст (дата, что сделано, показание).
            SERVICE: [CallbackQueryHandler(service_button, pattern=r"^s:"),
                      MessageHandler(FIELD_TEXT_FILTER, service_text)],
        },
        # fallbacks проверяются ПОСЛЕ обработчиков состояния, поэтому кнопки
        # нижнего меню и команды работают в любой момент, не мешая вводу текста.
        fallbacks=[CommandHandler("cancel", cancel),
                   CallbackQueryHandler(cancel, pattern=r"^cancel$"),
                   CommandHandler("start", start),
                   MessageHandler(filters.Regex(f"^{re.escape(BTN_UPDATE)}$"),
                                  start),
                   CommandHandler("to", start_service),
                   MessageHandler(filters.Regex(f"^{re.escape(BTN_SERVICE)}$"),
                                  start_service),
                   MessageHandler(filters.Regex(f"^{re.escape(BTN_SUMMARY)}$"),
                                  summary_command)],
    )


def main():
    app = (Application.builder().token(get_token())
           .connect_timeout(30).read_timeout(30)
           .get_updates_connect_timeout(30).get_updates_read_timeout(60)
           .post_init(post_init)
           .build())

    app.add_handler(build_conversation())
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("chatid", chat_id_command))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_SUMMARY)}$"),
                                   summary_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))

    if app.job_queue and SUMMARY_CHAT_ID:
        h, m = map(int, SUMMARY_TIME.split(":"))
        app.job_queue.run_daily(
            daily_summary_job, time=dt.time(h, m, tzinfo=ZoneInfo(TIMEZONE)))
        print(f"Сводка запланирована ежедневно в {SUMMARY_TIME} ({TIMEZONE})")
    elif not SUMMARY_CHAT_ID:
        print("SUMMARY_CHAT_ID не задан — автосводка отключена "
              "(ручная команда /summary работает)")

    if not PLATFORM_URL or not PLATFORM_KEY:
        raise SystemExit("Задайте переменные PLATFORM_URL и PLATFORM_KEY "
                         "(ключ — в платформе: Админ-панель → Интеграции)")
    print(f"Бот запущен. Платформа: {PLATFORM_URL}")
    app.run_polling()


if __name__ == "__main__":
    main()
