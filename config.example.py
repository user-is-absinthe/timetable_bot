# config.example.py
from pytz import timezone

TOKEN = "PUT_TOKEN_HERE"
CHAT_ID = -1001234567890
CSV_FILE = "timetable.csv"

TIMEZONE = timezone("Europe/Moscow")  # поменяй при необходимости

REMINDER_MORNING_TIME = (7, 30)
REMINDER_EVENING_TIME = (19, 30)
