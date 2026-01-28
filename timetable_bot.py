#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram-бот для напоминаний о занятиях из CSV.

Требования:
- python-telegram-bot >= 20
- Установить JobQueue: pip install "python-telegram-bot[job-queue]"
- pytz

Настройки вынесены в config.py (не коммитить), пример: config.example.py
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any

import pytz
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import config  # noqa: F401


# ========= НАСТРОЙКИ (из config.py) =========
TOKEN: str = config.TOKEN
CHAT_ID: int = config.CHAT_ID
CSV_FILE: str = config.CSV_FILE

TIMEZONE = config.TIMEZONE  # pytz timezone object
REMINDER_MORNING_TIME = config.REMINDER_MORNING_TIME  # (7, 30)
REMINDER_EVENING_TIME = config.REMINDER_EVENING_TIME  # (19, 30)

REMINDERS_FILE = getattr(config, "REMINDERS_FILE", "reminders.json")
MAX_REMINDERS_PER_USER = getattr(config, "MAX_REMINDERS_PER_USER", 20)

# Если True: /get_timetable БЕЗ даты показывает расписание на следующий учебный день (по CSV),
# а не просто на следующий будний день.
NEXT_DAY_MODE_USE_CSV = getattr(config, "NEXT_DAY_MODE_USE_CSV", True)

# Ограничение поиска "следующего учебного дня" вперед
MAX_LOOKAHEAD_DAYS = getattr(config, "MAX_LOOKAHEAD_DAYS", 365)


# ========= УТИЛИТЫ =========
DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def now_tz() -> datetime:
    return datetime.now(TIMEZONE)


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def parse_date_ddmmyyyy(s: str) -> datetime:
    return datetime.strptime(s, "%d.%m.%Y")


def format_date_ddmmyyyy(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")


def is_working_day(dt: datetime) -> bool:
    # 0..4 = Mon..Fri
    return dt.weekday() < 5


# ========= РАСПИСАНИЕ =========
@dataclass(frozen=True)
class LessonRow:
    date_str: str
    pair: int
    discipline: str
    theme: str
    kind: str
    teachers: str
    room: str

    @staticmethod
    def from_csv_row(row: Dict[str, str]) -> "LessonRow":
        def g(key: str) -> str:
            return (row.get(key) or "").strip()

        pair_s = g("Пара")
        try:
            pair_i = int(pair_s)
        except Exception:
            pair_i = 0

        return LessonRow(
            date_str=g("Дата"),
            pair=pair_i,
            discipline=g("Дисциплина"),
            theme=g("Номер темы"),
            kind=g("Вид занятия"),
            teachers=g("Преподаватели"),
            room=g("Ауд."),
        )


class Timetable:
    def __init__(self, csv_file: str):
        self.csv_file = csv_file
        self.by_date: Dict[str, List[LessonRow]] = {}
        self.load_csv()

    def load_csv(self) -> None:
        if not os.path.exists(self.csv_file):
            raise FileNotFoundError(f"CSV file not found: {self.csv_file}")

        with open(self.csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            by_date: Dict[str, List[LessonRow]] = {}
            for row in reader:
                lr = LessonRow.from_csv_row(row)
                if not lr.date_str:
                    continue
                by_date.setdefault(lr.date_str, []).append(lr)

        # сортировка по номеру пары
        for d, rows in by_date.items():
            by_date[d] = sorted(rows, key=lambda x: x.pair)

        self.by_date = by_date

    def get_rows_for_date(self, dt: datetime) -> List[LessonRow]:
        return self.by_date.get(format_date_ddmmyyyy(dt), [])

    def has_study_on_date(self, dt: datetime) -> bool:
        return len(self.get_rows_for_date(dt)) > 0

    def is_self_study_day(self, dt: datetime) -> bool:
        rows = self.get_rows_for_date(dt)
        if not rows:
            return False
        # самоподготовка: все дисциплины пустые
        return all((r.discipline or "").strip() == "" for r in rows)

    def format_timetable(self, dt: datetime) -> str:
        date_str = format_date_ddmmyyyy(dt)
        rows = self.get_rows_for_date(dt)

        if not rows:
            return f"📚 Расписание на {date_str}:\n\nПар нет."

        if self.is_self_study_day(dt):
            return f"📚 Расписание на {date_str}:\n\nРабота над диссертацией"

        lines: List[str] = [f"📚 Расписание на {date_str}:\n"]
        for r in rows:
            subject = r.discipline or "-"
            theme = r.theme or ""
            kind = r.kind or "-"
            teachers = r.teachers or "-"
            room = r.room or "-"

            s = f"{r.pair}. {subject}"
            if theme.strip():
                s += f" ({theme.strip()})"
            s += f" | {kind} | {teachers} | {room}"
            lines.append(s)

        return "\n".join(lines)

    def get_next_study_day(self, from_dt: Optional[datetime] = None) -> Optional[datetime]:
        """
        Ищет следующий "учебный день":
        - по умолчанию: ближайший будний день, который присутствует в CSV
        - если NEXT_DAY_MODE_USE_CSV=False: ближайший будний день (пн-пт) независимо от CSV
        """
        if from_dt is None:
            from_dt = now_tz()

        start = from_dt.replace(hour=0, minute=0, second=0, microsecond=0)

        for i in range(1, MAX_LOOKAHEAD_DAYS + 1):
            d = start + timedelta(days=i)

            if not is_working_day(d):
                continue

            if NEXT_DAY_MODE_USE_CSV:
                if self.has_study_on_date(d):
                    return d
            else:
                return d

        return None


# ========= НАПОМИНАНИЯ =========
@dataclass
class UserReminders:
    username: str  # без @
    items: List[str]


class ReminderStorage:
    """
    Хранение напоминаний "до ближайшего оглашения" (обычно это утреннее/вечернее авто-уведомление).

    Формат файла:
    {
      "users": {
        "12345": {"username": "ivan", "items": ["text1", "text2"]},
        "67890": {"username": "maria", "items": ["text1"]}
      }
    }
    """

    def __init__(self, storage_file: str):
        self.storage_file = storage_file
        self.users: Dict[int, UserReminders] = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.storage_file):
            self.users = {}
            return

        with open(self.storage_file, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}

        users_raw = raw.get("users", {}) or {}
        users: Dict[int, UserReminders] = {}
        for k, v in users_raw.items():
            try:
                uid = int(k)
            except Exception:
                continue
            username = (v.get("username") or "").strip()
            items = v.get("items") or []
            if not isinstance(items, list):
                items = []
            items = [normalize_text(str(x)) for x in items if normalize_text(str(x))]
            if items:
                users[uid] = UserReminders(username=username, items=items)

        self.users = users

    def save(self) -> None:
        raw = {
            "users": {
                str(uid): {"username": ur.username, "items": ur.items}
                for uid, ur in self.users.items()
            }
        }
        with open(self.storage_file, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    def add(self, user_id: int, username: str, text: str) -> int:
        text = normalize_text(text)
        if not text:
            return 0

        username = (username or "").lstrip("@").strip()

        ur = self.users.get(user_id)
        if ur is None:
            ur = UserReminders(username=username, items=[])
            self.users[user_id] = ur
        else:
            # обновим username на более актуальный, если появился
            if username:
                ur.username = username

        if len(ur.items) >= MAX_REMINDERS_PER_USER:
            return -1

        ur.items.append(text)
        self.save()
        return len(ur.items)

    def get_user_items(self, user_id: int) -> List[str]:
        ur = self.users.get(user_id)
        return list(ur.items) if ur else []

    def delete_one(self, user_id: int, index_1based: int) -> bool:
        ur = self.users.get(user_id)
        if not ur:
            return False
        idx = index_1based - 1
        if idx < 0 or idx >= len(ur.items):
            return False
        ur.items.pop(idx)
        if not ur.items:
            self.users.pop(user_id, None)
        self.save()
        return True

    def delete_all(self, user_id: int) -> bool:
        if user_id not in self.users:
            return False
        self.users.pop(user_id, None)
        self.save()
        return True

    def clear_all_users(self) -> None:
        self.users = {}
        self.save()

    def all_users(self) -> Dict[int, UserReminders]:
        return self.users


def format_reminders_block(users: Dict[int, UserReminders]) -> str:
    """
    Формат:
    @ivan:
    1. "..."
    2. "..."

    @maria:
    "..."
    """
    if not users:
        return ""

    parts: List[str] = []
    for _, ur in users.items():
        uname = ur.username or "username"
        parts.append(f"@{uname}:")
        if len(ur.items) == 1:
            parts.append(f"\"{ur.items[0]}\"")
        else:
            for i, text in enumerate(ur.items, 1):
                parts.append(f"{i}. \"{text}\"")
        parts.append("")  # пустая строка между пользователями
    return "\n".join(parts).rstrip()


# ========= БОТ =========
timetable = Timetable(CSV_FILE)
reminders = ReminderStorage(REMINDERS_FILE)

BTN_TIMETABLE = "📅 Расписание"
BTN_MY_REMINDERS = "⏰ Мои напоминания"
BTN_ADD_REMINDER = "➕ Добавить напоминание"
BTN_DEL_REMINDER = "🗑️ Удалить напоминание"

CB_DEL_ONE_PREFIX = "del_one:"
CB_DEL_ALL = "del_all"


def main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [BTN_TIMETABLE, BTN_MY_REMINDERS],
        [BTN_ADD_REMINDER, BTN_DEL_REMINDER],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    await update.effective_message.reply_text(
        "Меню бота готово.\n\n"
        f"Chat ID (для конфига): {chat_id}",
        reply_markup=main_keyboard(),
    )


async def cmd_get_timetable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /get_timetable [DD.MM.YYYY]
    args = context.args or []
    if args:
        ds = args[0].strip()
        if not DATE_RE.match(ds):
            await update.effective_message.reply_text(
                "❌ Неверный формат даты. Используй: /get_timetable 01.02.2026"
            )
            return
        dt = parse_date_ddmmyyyy(ds)
        # делаем дату в нашей TZ (только дата важна)
        dt = TIMEZONE.localize(dt)
        msg = timetable.format_timetable(dt)
        await update.effective_message.reply_text(msg)
        return

    next_day = timetable.get_next_study_day(now_tz())
    if not next_day:
        await update.effective_message.reply_text("Пар впереди не найдено в пределах расписания.")
        return

    msg = timetable.format_timetable(next_day)
    await update.effective_message.reply_text(msg)


async def cmd_set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /set_reminder "text"
    text = " ".join(context.args or [])
    text = text.strip().strip("\"").strip("'").strip()

    if not text:
        await update.effective_message.reply_text(
            "❌ Использование: /set_reminder \"Текст напоминания\""
        )
        return

    if len(text) > 500:
        await update.effective_message.reply_text("❌ Слишком длинно (макс 500 символов).")
        return

    user = update.effective_user
    username = (user.username or user.first_name or "user").strip()
    count = reminders.add(user.id, username=username, text=text)

    if count == -1:
        await update.effective_message.reply_text(
            f"❌ Достигнут лимит напоминаний ({MAX_REMINDERS_PER_USER})."
        )
        return

    await update.effective_message.reply_text(
        f"✅ Добавлено. Сейчас у тебя {count} напоминаний(я) до ближайшего оглашения."
    )


async def show_my_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    items = reminders.get_user_items(user.id)
    if not items:
        await update.effective_message.reply_text("📌 У тебя нет напоминаний.")
        return

    lines = ["📌 Твои напоминания:"]
    for i, t in enumerate(items, 1):
        lines.append(f"{i}. \"{t}\"")
    await update.effective_message.reply_text("\n".join(lines))


async def ask_add_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["waiting_for_reminder_text"] = True
    await update.effective_message.reply_text("✍️ Отправь текст напоминания одним сообщением.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()

    # кнопки
    if text == BTN_TIMETABLE:
        await cmd_get_timetable(update, context)
        return
    if text == BTN_MY_REMINDERS:
        await show_my_reminders(update, context)
        return
    if text == BTN_ADD_REMINDER:
        await ask_add_reminder(update, context)
        return
    if text == BTN_DEL_REMINDER:
        await show_delete_menu(update, context)
        return

    # ожидаем ввод напоминания
    if context.user_data.get("waiting_for_reminder_text"):
        context.user_data["waiting_for_reminder_text"] = False
        # добавляем напоминание
        user = update.effective_user
        reminder_text = normalize_text(text)
        if not reminder_text:
            await update.effective_message.reply_text("❌ Пустое напоминание не добавлено.")
            return
        if len(reminder_text) > 500:
            await update.effective_message.reply_text("❌ Слишком длинно (макс 500 символов).")
            return

        username = (user.username or user.first_name or "user").strip()
        count = reminders.add(user.id, username=username, text=reminder_text)

        if count == -1:
            await update.effective_message.reply_text(
                f"❌ Достигнут лимит напоминаний ({MAX_REMINDERS_PER_USER})."
            )
            return

        await update.effective_message.reply_text(
            f"✅ Добавлено. Сейчас у тебя {count} напоминаний(я) до ближайшего оглашения."
        )
        return

    # fallback
    await update.effective_message.reply_text(
        "Не понял сообщение.\n\n"
        "Команды:\n"
        "/get_timetable [ДД.ММ.ГГГГ]\n"
        "/set_reminder \"текст\""
    )


async def show_delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    items = reminders.get_user_items(user.id)
    if not items:
        await update.effective_message.reply_text("🗑️ У тебя нет напоминаний для удаления.")
        return

    buttons: List[List[InlineKeyboardButton]] = []
    for i, t in enumerate(items, 1):
        label = f"Удалить #{i}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"{CB_DEL_ONE_PREFIX}{i}")])
    buttons.append([InlineKeyboardButton("Удалить все", callback_data=CB_DEL_ALL)])

    await update.effective_message.reply_text(
        "🗑️ Выбери, что удалить:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    user = update.effective_user
    data = q.data or ""

    if data == CB_DEL_ALL:
        ok = reminders.delete_all(user.id)
        await q.edit_message_text("✅ Все напоминания удалены." if ok else "Нет напоминаний.")
        return

    if data.startswith(CB_DEL_ONE_PREFIX):
        n_s = data[len(CB_DEL_ONE_PREFIX):]
        try:
            n = int(n_s)
        except Exception:
            await q.edit_message_text("❌ Некорректный выбор.")
            return
        ok = reminders.delete_one(user.id, n)
        await q.edit_message_text("✅ Удалено." if ok else "❌ Не найдено.")
        return


# ========= АВТОУВЕДОМЛЕНИЯ =========
async def send_schedule_to_chat(target_date: datetime, *, label: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    label: строка для логов/отладки, например 'morning'/'evening'
    """
    if not is_working_day(target_date):
        return

    if not timetable.has_study_on_date(target_date):
        return

    msg = timetable.format_timetable(target_date)

    # приклеиваем "следующие напоминания" и очищаем их (т.к. они "к следующему уведомлению")
    all_users = reminders.all_users()
    if all_users:
        block = format_reminders_block(all_users)
        if block:
            msg = msg + "\n\n" + block
        reminders.clear_all_users()

    await context.bot.send_message(chat_id=CHAT_ID, text=msg)


async def job_morning(context: ContextTypes.DEFAULT_TYPE) -> None:
    # 7:30 рабочего дня — расписание на сегодня
    today = now_tz().replace(hour=0, minute=0, second=0, microsecond=0)
    await send_schedule_to_chat(today, label="morning", context=context)


async def job_evening(context: ContextTypes.DEFAULT_TYPE) -> None:
    # 19:30 — расписание на завтра, если завтра рабочий день
    tomorrow = (now_tz().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
    await send_schedule_to_chat(tomorrow, label="evening", context=context)


def schedule_jobs(application: Application) -> None:
    job_queue = application.job_queue
    if job_queue is None:
        raise RuntimeError(
            "JobQueue не доступен. Установи зависимости: "
            "pip install \"python-telegram-bot[job-queue]\""
        )

    morning_time = dtime(REMINDER_MORNING_TIME[0], REMINDER_MORNING_TIME[1], tzinfo=TIMEZONE)
    evening_time = dtime(REMINDER_EVENING_TIME[0], REMINDER_EVENING_TIME[1], tzinfo=TIMEZONE)

    # run_daily по умолчанию каждый день; внутри job_* мы дополнительно проверяем "рабочий день"
    job_queue.run_daily(job_morning, time=morning_time, name="morning_reminder")
    job_queue.run_daily(job_evening, time=evening_time, name="evening_reminder")


# ========= MAIN =========
async def main() -> None:
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("get_timetable", cmd_get_timetable))
    application.add_handler(CommandHandler("set_reminder", cmd_set_reminder))

    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    schedule_jobs(application)

    print("🤖 Bot started")
    print(f"⏰ TIMEZONE: {TIMEZONE}")

    await application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    asyncio.run(main())
