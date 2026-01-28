#!/usr/bin/env python3
"""
Telegram бот для напоминаний о учебных занятиях
"""

import os
import csv
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import asyncio
import pytz

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ================= КОНФИГУРАЦИЯ =================
CHAT_ID = -1001234567890  # Замени на ID чата: скопируй целое число, которое выведет бот при первом запуске
TOKEN = "YOUR_BOT_TOKEN"  # Замени на токен от @BotFather
CSV_FILE = "timetable.csv"  # Путь к CSV файлу с расписанием

# ЧАСОВОЙ ПОЯС - ОЧЕНЬ ВАЖНО!
# Установи свой часовой пояс (примеры: 'Europe/Moscow', 'UTC', 'Europe/London', 'America/New_York')
TIMEZONE = pytz.timezone('Europe/Moscow')  # МСК

# Часовые пояса (в формате часов:минут)
REMINDER_MORNING_TIME = (7, 30)    # 7:30 - напоминание про сегодня
REMINDER_EVENING_TIME = (19, 30)   # 19:30 - напоминание про завтра

# ================= КЛАССЫ И ФУНКЦИИ =================

class Timetable:
    """Класс для работы с расписанием"""
    
    def __init__(self, csv_file: str):
        self.data = []
        self.load_csv(csv_file)
    
    def load_csv(self, csv_file: str):
        """Загружает расписание из CSV файла"""
        if not os.path.exists(csv_file):
            print(f"Ошибка: файл {csv_file} не найден!")
            return
        
        try:
            with open(csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f, delimiter=';')
                self.data = list(reader)
        except Exception as e:
            print(f"Ошибка при загрузке CSV: {e}")
    
    def parse_date(self, date_str: str) -> datetime:
        """Парсит дату в формате ДД.МММ.ГГГГ"""
        return datetime.strptime(date_str, "%d.%m.%Y")
    
    def date_to_str(self, date: datetime) -> str:
        """Преобразует дату в строку ДД.МММ.ГГГГ"""
        return date.strftime("%d.%m.%Y")
    
    def is_working_day(self, date: datetime) -> bool:
        """Проверяет, является ли день рабочим (не выходной)"""
        # 5 = суббота, 6 = воскресенье
        return date.weekday() < 5
    
    def get_next_working_day(self, from_date: Optional[datetime] = None) -> datetime:
        """Возвращает следующий рабочий день"""
        if from_date is None:
            from_date = datetime.now(TIMEZONE)
        
        current = from_date.replace(hour=0, minute=0, second=0, microsecond=0)
        if current == from_date.replace(hour=0, minute=0, second=0, microsecond=0):
            current += timedelta(days=1)
        else:
            current += timedelta(days=1)
        
        while not self.is_working_day(current):
            current += timedelta(days=1)
        
        return current
    
    def get_timetable_for_date(self, date: datetime) -> List[Dict]:
        """Получает расписание на определенную дату"""
        date_str = self.date_to_str(date)
        classes = [row for row in self.data if row['Дата'] == date_str]
        return sorted(classes, key=lambda x: int(x['Пара']))
    
    def format_timetable(self, date: datetime) -> str:
        """Форматирует расписание для вывода"""
        classes = self.get_timetable_for_date(date)
        
        if not classes:
            return f"На {self.date_to_str(date)} пар не найдено."
        
        # Проверяем, это день только самоподготовки?
        all_self_study = all(row['Дисциплина'] == '' for row in classes)
        
        if all_self_study:
            return f"📚 {self.date_to_str(date)} (пт)\n\nРабота над диссертацией"
        
        lines = [f"📚 Расписание на {self.date_to_str(date)}:\n"]
        
        for cls in classes:
            pair_num = cls['Пара']
            subject = cls['Дисциплина'] or "-"
            theme = cls['Номер темы'] or "-"
            lesson_type = cls['Вид занятия'] or "-"
            teacher = cls['Преподаватели'] or "-"
            room = cls['Ауд.'] or "-"
            
            line = f"{pair_num}. {subject}"
            if theme and theme != "-":
                line += f" ({theme})"
            line += f" | {lesson_type} | {teacher} | {room}"
            
            lines.append(line)
        
        return "\n".join(lines)


class ReminderStorage:
    """Класс для хранения напоминаний"""
    
    def __init__(self, storage_file: str = "reminders.json"):
        self.storage_file = storage_file
        self.reminders: Dict[int, List[str]] = {}  # user_id -> список напоминаний
        self.announced_dates: List[str] = []
        self.load()
    
    def load(self):
        """Загружает напоминания из файла"""
        if os.path.exists(self.storage_file):
            try:
                with open(self.storage_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.reminders = {int(k): v for k, v in data.get('reminders', {}).items()}
                    self.announced_dates = data.get('announced_dates', [])
            except Exception as e:
                print(f"Ошибка при загрузке напоминаний: {e}")
    
    def save(self):
        """Сохраняет напоминания в файл"""
        try:
            data = {
                'reminders': self.reminders,
                'announced_dates': self.announced_dates
            }
            with open(self.storage_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Ошибка при сохранении напоминаний: {e}")
    
    def add_reminder(self, user_id: int, text: str):
        """Добавляет напоминание для пользователя"""
        if user_id not in self.reminders:
            self.reminders[user_id] = []
        self.reminders[user_id].append(text)
        self.save()
    
    def get_reminders(self, user_id: int) -> List[str]:
        """Получает все напоминания пользователя"""
        return self.reminders.get(user_id, [])
    
    def delete_all_reminders(self, user_id: int):
        """Удаляет все напоминания пользователя"""
        if user_id in self.reminders:
            del self.reminders[user_id]
            self.save()
    
    def delete_reminder(self, user_id: int, index: int):
        """Удаляет конкретное напоминание пользователя"""
        if user_id in self.reminders and 0 <= index < len(self.reminders[user_id]):
            self.reminders[user_id].pop(index)
            if not self.reminders[user_id]:
                del self.reminders[user_id]
            self.save()
    
    def get_all_reminders(self) -> Dict[int, List[str]]:
        """Получает все напоминания со всеми пользователями"""
        return self.reminders
    
    def clear_announced(self):
        """Очищает список объявленных дат"""
        self.announced_dates = []
        self.save()
    
    def mark_announced(self, date_str: str):
        """Отмечает дату как объявленную"""
        if date_str not in self.announced_dates:
            self.announced_dates.append(date_str)
            self.save()
    
    def is_announced_today(self, date_str: str) -> bool:
        """Проверяет, была ли дата уже объявлена"""
        return date_str in self.announced_dates


# ================= ГЛОБАЛЬНЫЕ ОБЪЕКТЫ =================
timetable = Timetable(CSV_FILE)
reminders = ReminderStorage()
user_names: Dict[int, str] = {}  # Кэш имен пользователей


# ================= КОМАНДЫ =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user = update.effective_user
    user_names[user.id] = user.first_name or "Пользователь"
    
    keyboard = [
        ["📅 Расписание", "⏰ Мои напоминания"],
        ["➕ Добавить напоминание", "🗑️ Удалить напоминание"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n\n"
        f"Я помогу тебе не забыть про учебные занятия.\n\n"
        f"Chat ID (для конфига): {update.effective_chat.id}",
        reply_markup=reply_markup
    )


async def get_timetable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /get_timetable [ДД.МММ.ГГГГ]"""
    user = update.effective_user
    user_names[user.id] = user.first_name or "Пользователь"
    
    # Парсим дату из аргументов
    target_date = None
    
    if context.args:
        try:
            date_str = context.args[0]
            target_date = timetable.parse_date(date_str)
        except ValueError:
            await update.message.reply_text("❌ Неверный формат даты. Используй: /get_timetable 01.02.2026")
            return
    else:
        # Если даты нет, берем следующий учебный день
        target_date = timetable.get_next_working_day()
    
    # Формируем ответ
    message = timetable.format_timetable(target_date)
    
    # Добавляем напоминания, если они есть
    date_str = timetable.date_to_str(target_date)
    if not reminders.is_announced_today(date_str) and reminders.get_all_reminders():
        reminders_text = format_reminders_output(reminders.get_all_reminders(), user_names)
        if reminders_text:
            message += "\n\n" + reminders_text
            reminders.mark_announced(date_str)
    
    await update.message.reply_text(message)


async def set_reminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /set_reminder "text" """
    user = update.effective_user
    user_names[user.id] = user.first_name or "Пользователь"
    
    if not context.args:
        await update.message.reply_text(
            "❌ Использование: /set_reminder \"Твоё напоминание\"\n\n"
            "Пример: /set_reminder \"Подготовить доклад\""
        )
        return
    
    reminder_text = " ".join(context.args).strip('"')
    
    if len(reminder_text) > 200:
        await update.message.reply_text("❌ Напоминание слишком длинное (макс. 200 символов)")
        return
    
    reminders.add_reminder(user.id, reminder_text)
    user_reminders = reminders.get_reminders(user.id)
    await update.message.reply_text(
        f"✅ Напоминание добавлено: '{reminder_text}'\n\n"
        f"У тебя {len(user_reminders)} напоминани{'е' if len(user_reminders) == 1 else 'й'}"
    )


def format_reminders_output(all_reminders: Dict[int, List[str]], user_names: Dict[int, str]) -> str:
    """Форматирует напоминания для вывода"""
    lines = []
    
    for user_id, user_reminders_list in all_reminders.items():
        if not user_reminders_list:
            continue
        
        username = user_names.get(user_id, f"User {user_id}")
        lines.append(f"@{username}:")
        
        if len(user_reminders_list) == 1:
            lines.append(f"\"{user_reminders_list[0]}\"")
        else:
            for i, reminder in enumerate(user_reminders_list, 1):
                lines.append(f"{i}. \"{reminder}\"")
        
        lines.append("")  # Пустая строка между пользователями
    
    return "\n".join(lines).rstrip()


# ================= КНОПКИ =================

async def button_timetable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка 'Расписание'"""
    user = update.effective_user
    user_names[user.id] = user.first_name or "Пользователь"
    
    # Отправляем форму для выбора даты
    keyboard = [
        [InlineKeyboardButton("Сегодня", callback_data="timetable_today")],
        [InlineKeyboardButton("Завтра", callback_data="timetable_tomorrow")],
        [InlineKeyboardButton("Следующий рабочий день", callback_data="timetable_next")],
        [InlineKeyboardButton("Указать дату", callback_data="timetable_custom")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("📅 Выбери дату:", reply_markup=reply_markup)


async def button_my_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка 'Мои напоминания'"""
    user = update.effective_user
    user_names[user.id] = user.first_name or "Пользователь"
    
    user_reminders = reminders.get_reminders(user.id)
    
    if user_reminders:
        lines = ["📌 Твои напоминания:\n"]
        for i, reminder in enumerate(user_reminders, 1):
            lines.append(f"{i}. \"{reminder}\"")
        await update.message.reply_text("\n".join(lines))
    else:
        await update.message.reply_text("📌 У тебя пока нет установленных напоминаний.")


async def button_add_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка 'Добавить напоминание'"""
    user = update.effective_user
    user_names[user.id] = user.first_name or "Пользователь"
    
    await update.message.reply_text(
        "✍️ Напиши свое напоминание (максимум 200 символов):\n\n"
        "Или используй команду: /set_reminder \"Твой текст\""
    )
    context.user_data['waiting_for_reminder'] = True


async def button_delete_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка 'Удалить напоминание'"""
    user = update.effective_user
    user_names[user.id] = user.first_name or "Пользователь"
    
    user_reminders = reminders.get_reminders(user.id)
    
    if not user_reminders:
        await update.message.reply_text("❌ У тебя нет напоминаний для удаления.")
        return
    
    if len(user_reminders) == 1:
        reminders.delete_all_reminders(user.id)
        await update.message.reply_text("🗑️ Напоминание удалено.")
    else:
        lines = ["🗑️ Какое напоминание удалить?\n"]
        for i, reminder in enumerate(user_reminders, 1):
            lines.append(f"{i}. \"{reminder}\"")
        lines.append("\nОтправь номер (например: 2) или 'все' для удаления всех")
        
        await update.message.reply_text("\n".join(lines))
        context.user_data['waiting_for_deletion'] = user.id


async def handle_text_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текста напоминания"""
    user = update.effective_user
    user_names[user.id] = user.first_name or "Пользователь"
    
    if context.user_data.get('waiting_for_reminder'):
        reminder_text = update.message.text.strip()
        
        if len(reminder_text) > 200:
            await update.message.reply_text("❌ Напоминание слишком длинное (макс. 200 символов)")
            return
        
        reminders.add_reminder(user.id, reminder_text)
        user_reminders = reminders.get_reminders(user.id)
        await update.message.reply_text(
            f"✅ Напоминание добавлено: '{reminder_text}'\n\n"
            f"У тебя {len(user_reminders)} напоминани{'е' if len(user_reminders) == 1 else 'й'}"
        )
        context.user_data['waiting_for_reminder'] = False


async def handle_deletion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка удаления напоминания"""
    user = update.effective_user
    user_names[user.id] = user.first_name or "Пользователь"
    
    if context.user_data.get('waiting_for_deletion') == user.id:
        text = update.message.text.strip().lower()
        user_reminders = reminders.get_reminders(user.id)
        
        if text == "все":
            reminders.delete_all_reminders(user.id)
            await update.message.reply_text("🗑️ Все напоминания удалены.")
        else:
            try:
                index = int(text) - 1
                if 0 <= index < len(user_reminders):
                    deleted = user_reminders[index]
                    reminders.delete_reminder(user.id, index)
                    await update.message.reply_text(f"🗑️ Удалено: \"{deleted}\"")
                else:
                    await update.message.reply_text("❌ Неверный номер")
            except ValueError:
                await update.message.reply_text("❌ Введи номер или 'все'")
        
        context.user_data['waiting_for_deletion'] = None


async def callback_timetable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок расписания"""
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    user_names[user.id] = user.first_name or "Пользователь"
    
    now = datetime.now(TIMEZONE)
    
    if query.data == "timetable_today":
        target_date = now
    elif query.data == "timetable_tomorrow":
        target_date = now + timedelta(days=1)
    elif query.data == "timetable_next":
        target_date = timetable.get_next_working_day(now)
    elif query.data == "timetable_custom":
        await query.edit_message_text(
            "📅 Отправь дату в формате: ДД.МММ.ГГГГ\n\n"
            "Пример: 01.02.2026"
        )
        context.user_data['waiting_for_date'] = True
        return
    else:
        return
    
    message = timetable.format_timetable(target_date)
    
    # Добавляем напоминания, если они есть
    date_str = timetable.date_to_str(target_date)
    if not reminders.is_announced_today(date_str) and reminders.get_all_reminders():
        reminders_text = format_reminders_output(reminders.get_all_reminders(), user_names)
        if reminders_text:
            message += "\n\n" + reminders_text
            reminders.mark_announced(date_str)
    
    await query.edit_message_text(message)


async def handle_custom_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кастомной даты"""
    user = update.effective_user
    user_names[user.id] = user.first_name or "Пользователь"
    
    if context.user_data.get('waiting_for_date'):
        try:
            target_date = timetable.parse_date(update.message.text.strip())
            message = timetable.format_timetable(target_date)
            
            # Добавляем напоминания, если они есть
            date_str = timetable.date_to_str(target_date)
            if not reminders.is_announced_today(date_str) and reminders.get_all_reminders():
                reminders_text = format_reminders_output(reminders.get_all_reminders(), user_names)
                if reminders_text:
                    message += "\n\n" + reminders_text
                    reminders.mark_announced(date_str)
            
            await update.message.reply_text(message)
            context.user_data['waiting_for_date'] = False
        except ValueError:
            await update.message.reply_text("❌ Неверный формат даты. Используй: ДД.МММ.ГГГГ")


# ================= АВТОМАТИЧЕСКИЕ НАПОМИНАНИЯ =================

async def scheduled_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Запускает запланированное напоминание"""
    now = datetime.now(TIMEZONE)
    target_hour, target_minute = context.job.data['time']
    
    # Определяем дату для напоминания
    if now.hour < target_hour or (now.hour == target_hour and now.minute < target_minute):
        # Утреннее напоминание - на сегодня
        target_date = now
    else:
        # Вечернее напоминание - на завтра
        target_date = now + timedelta(days=1)
    
    # Проверяем, это учебный день?
    classes = timetable.get_timetable_for_date(target_date)
    
    if not classes:
        return  # Нет пар на эту дату
    
    date_str = timetable.date_to_str(target_date)
    
    # Формируем сообщение
    message = timetable.format_timetable(target_date)
    
    # Добавляем напоминания
    if reminders.get_all_reminders():
        reminders_text = format_reminders_output(reminders.get_all_reminders(), user_names)
        if reminders_text:
            message += "\n\n" + reminders_text
    
    # Отправляем сообщение
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text=message)
        reminders.mark_announced(date_str)
    except Exception as e:
        print(f"Ошибка при отправке напоминания: {e}")


# ================= ГЛАВНАЯ ФУНКЦИЯ =================

async def main():
    """Главная функция для запуска бота"""
    # Создаем приложение
    application = Application.builder().token(TOKEN).build()
    
    # Обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("get_timetable", get_timetable_cmd))
    application.add_handler(CommandHandler("set_reminder", set_reminder_cmd))
    
    # Обработчики кнопок
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    application.add_handler(CallbackQueryHandler(callback_timetable, pattern="^timetable_"))
    
    # Планируем автоматические напоминания
    job_queue = application.job_queue
    
    # Утреннее напоминание в 7:30
    job_queue.run_daily(
        scheduled_reminder,
        time=datetime.combine(datetime.now().date(), datetime.min.time()).replace(
            hour=REMINDER_MORNING_TIME[0],
            minute=REMINDER_MORNING_TIME[1]
        ).time(),
        data={'time': REMINDER_MORNING_TIME},
        name='morning_reminder',
        tzinfo=TIMEZONE
    )
    
    # Вечернее напоминание в 19:30
    job_queue.run_daily(
        scheduled_reminder,
        time=datetime.combine(datetime.now().date(), datetime.min.time()).replace(
            hour=REMINDER_EVENING_TIME[0],
            minute=REMINDER_EVENING_TIME[1]
        ).time(),
        data={'time': REMINDER_EVENING_TIME},
        name='evening_reminder',
        tzinfo=TIMEZONE
    )
    
    print("🤖 Бот запущен!")
    print(f"⏰ Часовой пояс: {TIMEZONE}")
    
    # Запускаем бота
    await application.run_polling(allowed_updates=Update.ALL_TYPES)


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Общая обработка текста"""
    user = update.effective_user
    user_names[user.id] = user.first_name or "Пользователь"
    
    text = update.message.text.strip()
    
    # Проверяем кнопки
    if text == "📅 Расписание":
        await button_timetable(update, context)
    elif text == "⏰ Мои напоминания":
        await button_my_reminders(update, context)
    elif text == "➕ Добавить напоминание":
        await button_add_reminder(update, context)
    elif text == "🗑️ Удалить напоминание":
        await button_delete_reminder(update, context)
    elif context.user_data.get('waiting_for_reminder'):
        await handle_text_reminder(update, context)
    elif context.user_data.get('waiting_for_deletion'):
        await handle_deletion(update, context)
    elif context.user_data.get('waiting_for_date'):
        await handle_custom_date(update, context)
    else:
        await update.message.reply_text(
            "❓ Не знаю такую команду. Используй кнопки или команды:\n\n"
            "/get_timetable [ДД.МММ.ГГГГ]\n"
            "/set_reminder \"текст\"\n"
            "/start"
        )


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO)
    
    asyncio.run(main())
