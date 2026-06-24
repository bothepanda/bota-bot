import logging
import os
import re
from datetime import date, datetime, time, timedelta

import pytz
from notion_client import Client
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])

TASKS_DB = "b71836a1c3c84433801d2252d47a90bf"
DEADLINES_DB = "fc217ac183cd4bc0b985ad01ef938363"
CONTENT_DB = "f830908b50164930bbbba96b59dcc4af"

TZ = pytz.timezone("Asia/Almaty")

notion = Client(auth=NOTION_TOKEN)


# ── Notion helpers ──────────────────────────────────────────────────────────

def add_task(text: str) -> None:
    notion.pages.create(
        parent={"database_id": TASKS_DB},
        properties={
            "Name": {"title": [{"text": {"content": text}}]},
            "Date": {"date": {"start": date.today().isoformat()}},
            "Status": {"select": {"name": "Open"}},
        },
    )


def get_open_tasks() -> list:
    results = notion.databases.query(
        database_id=TASKS_DB,
        filter={
            "or": [
                {"property": "Status", "select": {"equals": "Open"}},
                {"property": "Status", "select": {"equals": "Unknown"}},
                {"property": "Status", "select": {"is_empty": True}},
            ]
        },
        sorts=[{"property": "Date", "direction": "ascending"}],
    )
    return results["results"]


def _fuzzy_match(query: str, candidate: str) -> float:
    q_words = set(w.lower() for w in query.split() if len(w) > 2)
    c_words = set(w.lower() for w in candidate.split() if len(w) > 2)
    if not q_words:
        return 0.0
    return len(q_words & c_words) / len(q_words)


def mark_done(task_name: str) -> bool:
    results = notion.databases.query(
        database_id=TASKS_DB,
        filter={
            "or": [
                {"property": "Status", "select": {"equals": "Open"}},
                {"property": "Status", "select": {"equals": "Unknown"}},
            ]
        },
    )
    if not results["results"]:
        return False

    best_page = None
    best_score = 0.0
    for page in results["results"]:
        titles = page["properties"]["Name"]["title"]
        title = titles[0]["plain_text"] if titles else ""
        score = _fuzzy_match(task_name, title)
        if score > best_score:
            best_score = score
            best_page = page

    if best_score < 0.4 or best_page is None:
        return False

    notion.pages.update(
        page_id=best_page["id"],
        properties={"Status": {"select": {"name": "Done"}}},
    )
    return True


def mark_unknown_if_open_yesterday() -> None:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    results = notion.databases.query(
        database_id=TASKS_DB,
        filter={
            "and": [
                {"property": "Status", "select": {"equals": "Open"}},
                {"property": "Date", "date": {"equals": yesterday}},
            ]
        },
    )
    for page in results["results"]:
        notion.pages.update(
            page_id=page["id"],
            properties={"Status": {"select": {"name": "Unknown"}}},
        )


def get_deadlines() -> list:
    results = notion.databases.query(
        database_id=DEADLINES_DB,
        sorts=[{"property": "Deadline", "direction": "ascending"}],
    )
    return results["results"]


def get_content_active() -> list:
    results = notion.databases.query(database_id=CONTENT_DB)
    return results["results"]


# ── Formatters ───────────────────────────────────────────────────────────────

def _task_title(page: dict) -> str:
    titles = page["properties"]["Name"]["title"]
    return titles[0]["plain_text"] if titles else "?"


def _page_title(page: dict) -> str:
    for key in ("Name", "Title", "Заголовок", "name"):
        prop = page["properties"].get(key)
        if prop and prop.get("title"):
            return prop["title"][0]["plain_text"]
    return "?"


def format_open_tasks(tasks: list) -> str:
    if not tasks:
        return "нет открытых задач ✨"
    today = date.today()
    lines = []
    for t in tasks:
        name = _task_title(t)
        status = (t["properties"].get("Status") or {}).get("select") or {}
        status_name = status.get("name", "Open")
        date_prop = (t["properties"].get("Date") or {}).get("date") or {}
        task_date_str = date_prop.get("start")

        if task_date_str:
            task_date = date.fromisoformat(task_date_str)
            diff = (today - task_date).days
            if diff == 0:
                label = ""
            elif diff == 1:
                label = " _(вчера)_"
            else:
                label = f" _({diff} дн. назад)_"
        else:
            label = ""

        if status_name == "Unknown":
            icon = "⚠️"
            suffix = " — статус неизвестен"
        else:
            icon = "🔲"
            suffix = ""

        lines.append(f"{icon} {name}{label}{suffix}")
    return "\n".join(lines)


def build_brief() -> str:
    try:
        tasks = get_open_tasks()
    except Exception as e:
        logger.error(f"build_brief tasks error: {e}")
        tasks = []
    try:
        deadlines = get_deadlines()
    except Exception as e:
        logger.error(f"build_brief deadlines error: {e}")
        deadlines = []
    try:
        content = get_content_active()
    except Exception as e:
        logger.error(f"build_brief content error: {e}")
        content = []

    now = datetime.now(TZ)
    day_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][now.weekday()]
    date_str = now.strftime(f"%d.%m.%Y, {day_ru}")

    parts = [f"☀️ *{date_str}*\n"]

    parts.append("*━━ ЗАДАЧИ ━━*")
    parts.append(format_open_tasks(tasks))
    parts.append("")

    parts.append("*━━ ДЕДЛАЙНЫ (30 дней) ━━*")
    if deadlines:
        for d in deadlines:
            props = d["properties"]
            name = _page_title(d)
            dl = (props.get("Deadline") or {}).get("date") or {}
            dl_start = dl.get("start", "?")
            if dl_start != "?":
                try:
                    dl_start = date.fromisoformat(dl_start).strftime("%d.%m")
                except ValueError:
                    pass
            parts.append(f"📅 {dl_start} — {name}")
    else:
        parts.append("нет дедлайнов в ближайшие 30 дней")
    parts.append("")

    parts.append("*━━ КОНТЕНТ В РАБОТЕ ━━*")
    if content:
        for c in content:
            parts.append(f"✍️ {_page_title(c)}")
    else:
        parts.append("нет активного контента")

    return "\n".join(parts)


RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def parse_ru_date(text: str) -> date | None:
    m = re.search(r"(\d{1,2})\s+(" + "|".join(RU_MONTHS) + r")(?:\s+(\d{4}))?", text, re.I)
    if not m:
        return None
    day = int(m.group(1))
    month = RU_MONTHS[m.group(2).lower()]
    year = int(m.group(3)) if m.group(3) else date.today().year
    try:
        return date(year, month, day)
    except ValueError:
        return None


def update_task_name_and_date(old_name: str, new_name: str, remind_date: date | None) -> bool:
    results = notion.databases.query(
        database_id=TASKS_DB,
        filter={
            "or": [
                {"property": "Status", "select": {"equals": "Open"}},
                {"property": "Status", "select": {"equals": "Unknown"}},
            ]
        },
    )
    best_page = None
    best_score = 0.0
    for page in results["results"]:
        titles = page["properties"]["Name"]["title"]
        title = titles[0]["plain_text"] if titles else ""
        score = _fuzzy_match(old_name, title)
        if score > best_score:
            best_score = score
            best_page = page

    if best_score < 0.3 or best_page is None:
        return False

    props: dict = {"Name": {"title": [{"text": {"content": new_name}}]}}
    if remind_date:
        props["Date"] = {"date": {"start": remind_date.isoformat()}}

    notion.pages.update(page_id=best_page["id"], properties=props)
    return True


# ── Scheduled jobs ───────────────────────────────────────────────────────────

async def job_morning_brief(context: ContextTypes.DEFAULT_TYPE) -> None:
    mark_unknown_if_open_yesterday()
    text = build_brief()
    text += "\n\n_Что сегодня планируешь? Напиши списком._"
    await context.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")


async def job_evening_checkin(context: ContextTypes.DEFAULT_TYPE) -> None:
    today = date.today().isoformat()
    results = notion.databases.query(
        database_id=TASKS_DB,
        filter={
            "and": [
                {"property": "Status", "select": {"equals": "Open"}},
                {"property": "Date", "date": {"equals": today}},
            ]
        },
    )
    tasks = results["results"]
    if not tasks:
        return

    lines = ["🌙 *Вечерняя сверка*\n\nЧто из этого сделала?"]
    for t in tasks:
        lines.append(f"🔲 {_task_title(t)}")
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text="\n".join(lines),
        parse_mode="Markdown",
        reply_markup=tasks_keyboard(tasks),
    )


# ── Handlers ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != CHAT_ID:
        return
    await update.message.reply_text(
        "Привет! Я твой планировщик дня.\n\n"
        "Просто напиши задачи — каждую на новой строке.\n\n"
        "Команды:\n"
        "/brief — брифинг прямо сейчас\n"
        "/tasks — открытые задачи с кнопками\n"
        "/skip — пропустить вечернюю сверку\n\n"
        "_Обновить задачу:_\n"
        "`обнови: who youth council → отправила заявку, проверить ответ 1 августа 2026`",
        parse_mode="Markdown",
    )


async def handle_update_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != CHAT_ID:
        return
    text = update.message.text.strip()
    # Format: "обнови: [old] → [new]"
    m = re.match(r"(?i)обнови[\s:–\-]+(.+?)\s*[→\-–]+\s*(.+)", text)
    if not m:
        await update.message.reply_text(
            "Формат: `обнови: [задача] → [новое название]`\n"
            "Можно добавить дату: `... проверить ответ 1 августа 2026`",
            parse_mode="Markdown",
        )
        return

    old_name = m.group(1).strip()
    new_name = m.group(2).strip()
    remind_date = parse_ru_date(new_name)

    if update_task_name_and_date(old_name, new_name, remind_date):
        reply = f"✏️ Обновила: _{new_name}_"
        if remind_date:
            reply += f"\n📅 Напомню {remind_date.strftime('%d.%m.%Y')}"
        await update.message.reply_text(reply, parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"Не нашла задачу «{old_name}». Напиши /tasks чтобы увидеть список."
        )


def tasks_keyboard(tasks: list) -> InlineKeyboardMarkup:
    buttons = []
    for t in tasks:
        page_id = t["id"].replace("-", "")
        name = _task_title(t)
        short = name[:30] + "…" if len(name) > 30 else name
        buttons.append([InlineKeyboardButton(f"✅ {short}", callback_data=f"done:{page_id}")])
    return InlineKeyboardMarkup(buttons)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != CHAT_ID:
        return
    tasks = get_open_tasks()
    if not tasks:
        await update.message.reply_text("нет открытых задач ✨")
        return
    await update.message.reply_text(
        "*Открытые задачи:*\n" + format_open_tasks(tasks),
        parse_mode="Markdown",
        reply_markup=tasks_keyboard(tasks),
    )


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != CHAT_ID:
        return
    try:
        text = build_brief()
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"cmd_brief error: {e}")
        await update.message.reply_text(f"Ошибка при сборке брифинга: {e}")


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != CHAT_ID:
        return
    await update.message.reply_text("Окей 🌙")


async def handle_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != CHAT_ID:
        return
    text = update.message.text.strip()
    for prefix in ("готово:", "сделала:", "done:", "✓"):
        if text.lower().startswith(prefix):
            task_name = text[len(prefix):].strip()
            break
    else:
        task_name = text

    if mark_done(task_name):
        await update.message.reply_text(f"✅ Закрыла: {task_name}")
    else:
        await update.message.reply_text(
            f"Не нашла задачу «{task_name}».\nПроверь название или напиши /tasks."
        )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("done:"):
        return
    page_id = data[5:]
    # re-insert dashes: 8-4-4-4-12
    page_id = f"{page_id[:8]}-{page_id[8:12]}-{page_id[12:16]}-{page_id[16:20]}-{page_id[20:]}"
    try:
        page = notion.pages.retrieve(page_id)
        titles = page["properties"]["Name"]["title"]
        name = titles[0]["plain_text"] if titles else "задача"
        notion.pages.update(
            page_id=page_id,
            properties={"Status": {"select": {"name": "Done"}}},
        )
        await query.edit_message_text(f"✅ Закрыла: {name}")
    except Exception:
        await query.edit_message_text("Не удалось закрыть задачу.")


async def handle_tasks_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != CHAT_ID:
        return
    text = update.message.text.strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return

    added = []
    for item in lines:
        add_task(item)
        added.append(item)

    if len(added) == 1:
        reply = f"🔲 {added[0]}"
    else:
        reply = "Добавила:\n" + "\n".join(f"🔲 {l}" for l in added)

    tasks = get_open_tasks()
    await update.message.reply_text(
        reply, reply_markup=tasks_keyboard(tasks)
    )


# ── Main ──────────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("brief", "☀️ Брифинг на сегодня"),
        BotCommand("tasks", "🔲 Открытые задачи"),
        BotCommand("skip", "🌙 Пропустить вечернюю сверку"),
    ])
    await app.bot.set_chat_menu_button(
        chat_id=CHAT_ID, menu_button=MenuButtonCommands()
    )


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("brief", cmd_brief))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CallbackQueryHandler(handle_button, pattern=r"^done:"))
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"(?i)^обнови[:\s]"),
            handle_update_task,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"(?i)^(готово|сделала|done|✓):"),
            handle_done,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tasks_input))

    jq = app.job_queue
    jq.run_daily(job_morning_brief, time=time(6, 0, tzinfo=TZ))
    jq.run_daily(job_evening_checkin, time=time(21, 0, tzinfo=TZ))

    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
