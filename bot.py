import logging
import os
from datetime import date, datetime, time, timedelta

import pytz
from notion_client import Client
from telegram import Update
from telegram.ext import (
    Application,
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
DEADLINES_DB = "af78e67c-8046-4a22-9049-e57446f965f9"
CONTENT_DB = "ad4f32a7-9762-4606-9438-098b66d27697"

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


def mark_done(task_name: str) -> bool:
    results = notion.databases.query(
        database_id=TASKS_DB,
        filter={
            "and": [
                {"property": "Name", "rich_text": {"contains": task_name}},
                {
                    "or": [
                        {"property": "Status", "select": {"equals": "Open"}},
                        {"property": "Status", "select": {"equals": "Unknown"}},
                    ]
                },
            ]
        },
    )
    if not results["results"]:
        return False
    notion.pages.update(
        page_id=results["results"][0]["id"],
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
    today = date.today()
    future = (today + timedelta(days=30)).isoformat()
    results = notion.databases.query(
        database_id=DEADLINES_DB,
        filter={
            "and": [
                {"property": "Deadline", "date": {"on_or_after": today.isoformat()}},
                {"property": "Deadline", "date": {"on_or_before": future}},
            ]
        },
        sorts=[{"property": "Deadline", "direction": "ascending"}],
    )
    return results["results"]


def get_content_active() -> list:
    results = notion.databases.query(
        database_id=CONTENT_DB,
        filter={
            "or": [
                {"property": "Status", "select": {"equals": "In Progress"}},
                {"property": "Status", "select": {"equals": "Draft"}},
                {"property": "Status", "select": {"equals": "drafting"}},
            ]
        },
    )
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
    tasks = get_open_tasks()
    deadlines = get_deadlines()
    content = get_content_active()

    now = datetime.now(TZ)
    day_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][now.weekday()]
    date_str = now.strftime(f"%d.%m.%Y, {day_ru}")

    parts = [f"☀️ *{date_str}*\n"]

    parts.append("*━━ ЗАДАЧИ ━━*")
    parts.append(format_open_tasks(tasks))
    parts.append("")

    parts.append("*━━ ДЕДЛАЙНЫ (30 дней) ━━*")
    if deadlines:
        for d in deadlines[:6]:
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
        for c in content[:4]:
            parts.append(f"✍️ {_page_title(c)}")
    else:
        parts.append("нет активного контента")

    return "\n".join(parts)


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

    lines = ["🌙 *Вечерняя сверка*\n\nЧто из этого сделала?\n"]
    for t in tasks:
        lines.append(f"🔲 {_task_title(t)}")
    lines.append("\n_Напиши 'готово: [задача]' или /skip_")
    await context.bot.send_message(
        chat_id=CHAT_ID, text="\n".join(lines), parse_mode="Markdown"
    )


# ── Handlers ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != CHAT_ID:
        return
    await update.message.reply_text(
        "Привет! Я твой планировщик дня.\n\n"
        "Просто напиши задачи — каждую на новой строке.\n"
        "Когда сделала — напиши: `готово: название`\n\n"
        "Команды:\n"
        "/tasks — открытые задачи\n"
        "/brief — брифинг прямо сейчас\n"
        "/skip — пропустить вечернюю сверку",
        parse_mode="Markdown",
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != CHAT_ID:
        return
    tasks = get_open_tasks()
    await update.message.reply_text(
        "*Открытые задачи:*\n" + format_open_tasks(tasks), parse_mode="Markdown"
    )


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != CHAT_ID:
        return
    await update.message.reply_text(build_brief(), parse_mode="Markdown")


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


async def handle_tasks_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != CHAT_ID:
        return
    text = update.message.text.strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return

    for item in lines:
        add_task(item)

    if len(lines) == 1:
        reply = f"🔲 Добавила: {lines[0]}\n\n_Напиши 'готово: {lines[0]}' когда сделаешь._"
    else:
        bullet = "\n".join(f"🔲 {l}" for l in lines)
        reply = f"Добавила {len(lines)} задачи:\n{bullet}"

    await update.message.reply_text(reply, parse_mode="Markdown")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("brief", cmd_brief))
    app.add_handler(CommandHandler("skip", cmd_skip))
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

    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
