# -*- coding: utf-8 -*-
"""Equipment status bot: step-by-step updates + daily summary + russian menu.

v5 (Railway-ready): вся конфигурация через переменные окружения.

Переменные окружения (Railway -> Variables):
    BOT_TOKEN          - токен бота от BotFather (обязательно)
    GOOGLE_CREDS_JSON  - содержимое service_account.json целиком (обязательно)
    SUMMARY_CHAT_ID    - ID группового чата для сводки (например -1001234567890)
    SUMMARY_TIME       - время сводки, по умолчанию 18:00
    TIMEZONE           - по умолчанию Asia/Atyrau
    ALLOWED_USERS      - ID через запятую (пусто = разрешено всем)

Локальный запуск по-прежнему работает от файлов
service_account.json и bot_token.txt рядом со скриптом.
"""
import datetime as dt
import html
import json
import os
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, MessageHandler,
                          filters)

# --- Настройки (env-переменные с запасными значениями) --------------------

SPREADSHEET_ID = "1YoEfMJ1jl9qX9oycjZaI7ns9J2fVZ2f3iQexMz-Nn3A"
SERVICE_ACCOUNT_FILE = "service_account.json"
TOKEN_FILE = "bot_token.txt"

ALLOWED_USERS = [int(x) for x in
                 os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x]

SUMMARY_CHAT_ID = int(os.getenv("SUMMARY_CHAT_ID", "0") or "0")
SUMMARY_TIME = os.getenv("SUMMARY_TIME", "18:00")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Atyrau")

STATUS_LIST = ["В работе", "В резерве", "В ремонте", "Ожидание", "Новая"]
PROJECT_LIST = ["ЭМГ", "CIS", "КРС", "Простой"]

BASE_SHEET = "База"
LOG_SHEET = "Журнал"


def now_local():
    """Текущее время в часовом поясе TIMEZONE (на хостинге сервер живёт в UTC)."""
    return datetime.now(ZoneInfo(TIMEZONE))


COL = {"Текущая локация": 8, "Проект": 9, "Статус": 10, "Текущее состояние": 11,
       "Последний ремонт": 12, "Примечание": 14, "Дата обновления": 15,
       "Кто обновил": 16}

KEY = {"Статус": "status", "Проект": "project", "Текущая локация": "location",
       "Текущее состояние": "condition", "Последний ремонт": "repair",
       "Примечание": "note"}

(CATEGORY, UNIT, UNIT_MENU, STATUS, PROJECT, PROJECT_TEXT,
 LOCATION, CONDITION, REPAIR, NOTE, CONFIRM) = range(11)

# --- Меню ------------------------------------------------------------------

BTN_UPDATE = "📝 Обновить статус"
BTN_SUMMARY = "📋 Сводка"

MENU_KB = ReplyKeyboardMarkup([[BTN_UPDATE, BTN_SUMMARY]],
                              resize_keyboard=True, is_persistent=True)

BOT_COMMANDS = [
    BotCommand("start", "📝 Обновить статус техники"),
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


# --- Google Sheets --------------------------------------------------------

_gc = None


def spreadsheet():
    global _gc
    if _gc is None:
        creds = os.getenv("GOOGLE_CREDS_JSON")
        if creds:
            _gc = gspread.service_account_from_dict(json.loads(creds))
        else:
            _gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    return _gc.open_by_key(SPREADSHEET_ID)


def load_units():
    rows = spreadsheet().worksheet(BASE_SHEET).get_all_values()
    units = []
    for i, r in enumerate(rows[1:], start=2):
        if len(r) >= 14 and r[0].strip():
            units.append({
                "row": i, "code": r[0].strip(), "category": r[1].strip(),
                "brand": r[3].strip(), "gos": r[5].strip(),
                "location": r[7].strip(), "project": r[8].strip(),
                "status": r[9].strip(), "condition": r[10].strip(),
                "repair": r[11].strip(), "note": r[13].strip(),
            })
    return units


def apply_changes(unit, changes, who):
    """Обновляет строку «Базы» и добавляет ОДНУ строку-снимок в «Журнал»."""
    ss = spreadsheet()
    base = ss.worksheet(BASE_SHEET)
    log = ss.worksheet(LOG_SHEET)
    now = now_local().strftime("%d.%m.%Y %H:%M")

    cells = []
    for field, new in changes.items():
        cells.append(gspread.Cell(unit["row"], COL[field], new))
    cells.append(gspread.Cell(unit["row"], COL["Дата обновления"], now.split()[0]))
    cells.append(gspread.Cell(unit["row"], COL["Кто обновил"], who))
    base.update_cells(cells, value_input_option="RAW")

    def val(field):
        return changes.get(field, unit[KEY[field]])

    log.append_row(
        [now, unit["code"], unit["brand"], val("Статус"), val("Проект"),
         val("Текущая локация"), val("Текущее состояние"),
         val("Последний ремонт"), val("Примечание"), who],
        value_input_option="RAW")


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
    units = load_units()
    log = spreadsheet().worksheet(LOG_SHEET).get_all_values()[1:]
    today = now_local().strftime("%d.%m.%Y")

    by_code = {u["code"]: u for u in units}
    st = Counter(u["status"] for u in units)
    idle = sum(1 for u in units if u["project"] == "Простой")

    lines = [f"📋 <b>Сводка по технике за {today}</b>", "",
             f"🟢 В работе: {st.get('В работе', 0)}   "
             f"🟡 В резерве: {st.get('В резерве', 0)}   "
             f"🔴 В ремонте: {st.get('В ремонте', 0)}   "
             f"⏸ Простой: {idle}"]

    changed_codes = []
    for r in log:
        if len(r) >= 2 and r[0].startswith(today) and r[1].strip():
            if r[1].strip() not in changed_codes:
                changed_codes.append(r[1].strip())

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


async def send_summary_to(bot, chat_id):
    text = build_summary()
    while text:
        chunk, text = text[:4000], text[4000:]
        await bot.send_message(chat_id, chunk, parse_mode="HTML")


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


async def send(update, text, markup=None):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


# --- Диалог обновления ----------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await update.message.reply_text(f"Ошибка чтения таблицы: {e}")
        return ConversationHandler.END

    context.user_data["units"] = units
    cats = []
    for u in units:
        if u["category"] not in cats:
            cats.append(u["category"])
    context.user_data["cats"] = cats

    buttons = [btn(c, f"cat:{i}") for i, c in enumerate(cats)] + [cancel_row()]
    await send(update, "Выберите категорию техники:", kb(buttons, 2))
    return CATEGORY


async def pick_category(update, context):
    idx = int(update.callback_query.data.split(":")[1])
    cat = context.user_data["cats"][idx]
    units = [u for u in context.user_data["units"] if u["category"] == cat]
    buttons = [btn(f"{u['code']} · {u['brand'][:28]}", f"unit:{u['code']}")
               for u in units]
    buttons += [btn("⬅️ Назад", "back"), cancel_row()]
    await send(update, f"<b>{html.escape(cat)}</b> — выберите технику:",
               kb(buttons, 1))
    return UNIT


async def back_to_categories(update, context):
    cats = context.user_data["cats"]
    buttons = [btn(c, f"cat:{i}") for i, c in enumerate(cats)] + [cancel_row()]
    await send(update, "Выберите категорию техники:", kb(buttons, 2))
    return CATEGORY


async def pick_unit(update, context):
    code = update.callback_query.data.split(":")[1]
    unit = next(u for u in context.user_data["units"] if u["code"] == code)
    context.user_data["unit"] = unit
    context.user_data["changes"] = {}
    buttons = [btn("✏️ Обновить", "edit"), btn("⬅️ Назад", "back"), cancel_row()]
    await send(update, unit_card(unit), kb(buttons, 2))
    return UNIT_MENU


async def begin_update(update, context):
    buttons = [btn(s, f"st:{s}") for s in STATUS_LIST]
    buttons += [btn("⏭ Пропустить", "st:-"), cancel_row()]
    await send(update, "1/6 · <b>Статус</b> — выберите новый:", kb(buttons, 2))
    return STATUS


async def set_status(update, context):
    val = update.callback_query.data.split(":", 1)[1]
    if val != "-":
        context.user_data["changes"]["Статус"] = val
    buttons = [btn(p, f"pr:{p}") for p in PROJECT_LIST]
    buttons += [btn("✍️ Другой…", "pr:+"), btn("⏭ Пропустить", "pr:-"), cancel_row()]
    await send(update, "2/6 · <b>Проект</b> — выберите:", kb(buttons, 2))
    return PROJECT


async def set_project(update, context):
    val = update.callback_query.data.split(":", 1)[1]
    if val == "+":
        await send(update, "Введите название проекта:", kb([cancel_row()], 1))
        return PROJECT_TEXT
    if val != "-":
        context.user_data["changes"]["Проект"] = val
    return await ask_location(update, context)


async def set_project_text(update, context):
    context.user_data["changes"]["Проект"] = update.message.text.strip()
    return await ask_location(update, context)


async def ask_location(update, context):
    await send(update, "3/6 · <b>Текущая локация</b> — напишите текстом:",
               kb([btn("⏭ Пропустить", "skip"), cancel_row()], 2))
    return LOCATION


async def set_location(update, context):
    context.user_data["changes"]["Текущая локация"] = update.message.text.strip()
    return await ask_condition(update, context)


async def skip_location(update, context):
    return await ask_condition(update, context)


async def ask_condition(update, context):
    await send(update, "4/6 · <b>Текущее состояние</b> — нажмите кнопку "
               "или опишите дефект текстом:",
               kb([btn("✅ Без дефектов", "ok"),
                   btn("⏭ Пропустить", "skip"), cancel_row()], 2))
    return CONDITION


async def condition_button(update, context):
    if update.callback_query.data == "ok":
        context.user_data["changes"]["Текущее состояние"] = "Без дефектов"
    return await ask_repair(update, context)


async def set_condition(update, context):
    context.user_data["changes"]["Текущее состояние"] = update.message.text.strip()
    return await ask_repair(update, context)


async def ask_repair(update, context):
    await send(update, "5/6 · <b>Последний ремонт</b> — напишите текстом:",
               kb([btn("⏭ Пропустить", "skip"), cancel_row()], 2))
    return REPAIR


async def set_repair(update, context):
    context.user_data["changes"]["Последний ремонт"] = update.message.text.strip()
    return await ask_note(update, context)


async def skip_repair(update, context):
    return await ask_note(update, context)


async def ask_note(update, context):
    await send(update, "6/6 · <b>Примечание</b> — напишите текстом:",
               kb([btn("⏭ Пропустить", "skip"), cancel_row()], 2))
    return NOTE


async def set_note(update, context):
    context.user_data["changes"]["Примечание"] = update.message.text.strip()
    return await show_confirm(update, context)


async def skip_note(update, context):
    return await show_confirm(update, context)


async def show_confirm(update, context):
    unit = context.user_data["unit"]
    changes = context.user_data["changes"]
    if not changes:
        await send(update, "Вы пропустили все поля — изменений нет.\n"
                   "Нажмите «📝 Обновить статус», чтобы начать заново.")
        return ConversationHandler.END

    lines = [f"<b>{unit['code']} — {html.escape(unit['brand'])}</b>", ""]
    for field, new in changes.items():
        old = unit[KEY[field]] or "—"
        lines.append(f"▸ {field}: <s>{html.escape(old)}</s> → "
                     f"<b>{html.escape(new)}</b>")
    lines.append("\nЗаписать изменения?")
    await send(update, "\n".join(lines),
               kb([btn("✅ Подтвердить", "yes"), btn("❌ Отмена", "no")], 2))
    return CONFIRM


async def confirm(update, context):
    if update.callback_query.data == "no":
        await send(update, "Отменено. Ничего не записано.")
        return ConversationHandler.END
    unit = context.user_data["unit"]
    changes = context.user_data["changes"]
    try:
        apply_changes(unit, changes, who_is(update))
    except Exception as e:
        await send(update, f"⚠️ Ошибка записи в таблицу: {e}\n"
                   "Изменения НЕ сохранены. Попробуйте ещё раз.")
        return ConversationHandler.END
    await send(update, f"✅ Записано: {unit['code']} — обновлено полей: "
               f"{len(changes)}.\nНажмите «📝 Обновить статус», чтобы "
               "обновить ещё одну машину.")
    return ConversationHandler.END


async def cancel(update, context):
    await send(update, "Отменено. Нажмите «📝 Обновить статус», чтобы начать заново.")
    return ConversationHandler.END


async def my_id(update, context):
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def unknown_text(update, context):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "Я жду нажатия кнопки. Если запутались — /cancel, затем "
            "«📝 Обновить статус».")


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


def main():
    app = (Application.builder().token(get_token())
           .connect_timeout(30).read_timeout(30)
           .get_updates_connect_timeout(30).get_updates_read_timeout(60)
           .post_init(post_init)
           .build())

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(f"^{BTN_UPDATE}$"), start),
        ],
        states={
            CATEGORY: [CallbackQueryHandler(pick_category, pattern=r"^cat:")],
            UNIT: [CallbackQueryHandler(pick_unit, pattern=r"^unit:"),
                   CallbackQueryHandler(back_to_categories, pattern=r"^back$")],
            UNIT_MENU: [CallbackQueryHandler(begin_update, pattern=r"^edit$"),
                        CallbackQueryHandler(back_to_categories, pattern=r"^back$")],
            STATUS: [CallbackQueryHandler(set_status, pattern=r"^st:")],
            PROJECT: [CallbackQueryHandler(set_project, pattern=r"^pr:")],
            PROJECT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                          set_project_text)],
            LOCATION: [CallbackQueryHandler(skip_location, pattern=r"^skip$"),
                       MessageHandler(filters.TEXT & ~filters.COMMAND, set_location)],
            CONDITION: [CallbackQueryHandler(condition_button, pattern=r"^(ok|skip)$"),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, set_condition)],
            REPAIR: [CallbackQueryHandler(skip_repair, pattern=r"^skip$"),
                     MessageHandler(filters.TEXT & ~filters.COMMAND, set_repair)],
            NOTE: [CallbackQueryHandler(skip_note, pattern=r"^skip$"),
                   MessageHandler(filters.TEXT & ~filters.COMMAND, set_note)],
            CONFIRM: [CallbackQueryHandler(confirm, pattern=r"^(yes|no)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel),
                   CallbackQueryHandler(cancel, pattern=r"^cancel$"),
                   CommandHandler("start", start)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("chatid", chat_id_command))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(MessageHandler(filters.Regex(f"^{BTN_SUMMARY}$"),
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

    print("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
