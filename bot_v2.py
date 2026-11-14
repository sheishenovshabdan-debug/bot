#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Телеграм-бот записи в мастерскую по ремонту телефонов (aiogram 3).

Что умеет:
  • запись: услуга → модель → день → время → имя → телефон;
  • «Мои записи»: посмотреть, отменить, перенести;
  • цены, контакты, помощь;
  • заявка приходит владельцу с кнопками «Подтвердить / Отклонить»;
  • напоминания накануне вечером и за пару часов до визита;
  • админка: /admin, /today, /tomorrow, /week, /stats, /find, /export, /broadcast.

Запуск:
    pip install -r requirements.txt
    python bot_v2.py

Все настройки берутся из переменных окружения (можно положить их в файл .env).
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

try:  # python-dotenv — необязательная зависимость
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


# ============================== НАСТРОЙКИ ==============================

def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        logging.warning("Переменная %s должна быть числом, беру %s", name, default)
        return default


BOT_TOKEN = _env("BOT_TOKEN")
ADMIN_ID = _env_int("ADMIN_ID", 0)

DB_PATH = _env("DB_PATH", "bookings.db")
LOG_PATH = _env("LOG_PATH", "bot.log")
TZ = ZoneInfo(_env("BOT_TZ", "Asia/Yekaterinburg"))

SHOP_NAME = _env("SHOP_NAME", "СитиАкс")
ADDRESS = _env("ADDRESS", "ул. Академика Сахарова, 107/1")
PHONE = _env("PHONE", "+7 932 127-44-92")
WORK_TEXT = _env("WORK_TEXT", "Ежедневно 11:00-22:00, без выходных")
TELEGRAM_URL = _env("TELEGRAM_URL", "https://t.me/SHVHSA")
MAPS_URL = _env("MAPS_URL", "")  # необязательно: ссылка на карту

# (название, цена, срок). Цены примерные — поставьте свои.
SERVICES: list[tuple[str, str, str]] = [
    ("Замена экрана", "от 3000 ₽", "около 1 часа"),
    ("Замена батареи", "от 1500 ₽", "30-40 минут"),
    ("Разъём зарядки", "от 1200 ₽", "около 1 часа"),
    ("Чистка после воды", "от 1500 ₽", "1-2 дня"),
    ("Диагностика", "бесплатно", "15-20 минут"),
]

WORK_HOURS = range(_env_int("WORK_START", 11), _env_int("WORK_END", 22))  # 11..21
DAYS_AHEAD = _env_int("DAYS_AHEAD", 7)
DAYS_OFF = {int(x) for x in _env("DAYS_OFF", "").split(",") if x.strip().isdigit()}
LEAD_MINUTES = _env_int("LEAD_MINUTES", 60)      # нельзя записаться «за 10 минут»
MAX_ACTIVE = _env_int("MAX_ACTIVE", 3)           # активных записей на одного человека
REMINDER_TICK = _env_int("REMINDER_TICK", 300)   # как часто проверять напоминания, сек
REMIND_EVENING_HOUR = _env_int("REMIND_EVENING_HOUR", 18)
REMIND_SOON_HOURS = _env_int("REMIND_SOON_HOURS", 2)

ACTIVE_STATUSES = ("pending", "confirmed")
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

BTN_BOOK = "📝 Записаться"
BTN_MY = "📋 Мои записи"
BTN_PRICES = "💰 Цены"
BTN_CONTACTS = "📍 Контакты"
BTN_HELP = "❓ Помощь"
BTN_ADMIN = "🛠 Админка"

logging.basicConfig(level=logging.INFO)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
router = Router()


# ============================== ВРЕМЯ И ТЕКСТ ==============================

def now() -> datetime:
    """Текущее время в часовом поясе салона (а не в поясе сервера)."""
    return datetime.now(TZ)


def today_str() -> str:
    return now().date().isoformat()


def iso_now() -> str:
    return now().isoformat(timespec="seconds")


def slot_times() -> list[str]:
    return [f"{h:02d}:00" for h in WORK_HOURS]


def slot_dt(day: str, t: str) -> datetime:
    d = date.fromisoformat(day)
    hh, mm = (int(x) for x in t.split(":"))
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ)


def fmt_day(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d:%d.%m} ({WEEKDAYS[d.weekday()]})"


def day_human(day: str) -> str:
    delta = (date.fromisoformat(day) - now().date()).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    return fmt_day(day)


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def fmt_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits[0] in "78":
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return phone or ""


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if not 10 <= len(digits) <= 15:
        return None
    return "+" + digits


def clean_name(raw: str) -> str | None:
    value = " ".join((raw or "").split())
    if not 2 <= len(value) <= 50:
        return None
    if len(re.findall(r"[A-Za-zА-Яа-яЁё]", value)) < 2:
        return None
    return value


def clean_model(raw: str) -> str | None:
    value = " ".join((raw or "").split())
    if not 2 <= len(value) <= 60:
        return None
    if not re.search(r"[A-Za-zА-Яа-яЁё0-9]", value):
        return None
    return value


def is_admin(user) -> bool:
    return bool(ADMIN_ID) and getattr(user, "id", None) == ADMIN_ID


def is_bookable_day(day: str) -> bool:
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return False
    today = now().date()
    if not today <= d <= today + timedelta(days=DAYS_AHEAD - 1):
        return False
    return d.weekday() not in DAYS_OFF


# ============================== БАЗА ==============================

TABLE_BOOKINGS = """
CREATE TABLE IF NOT EXISTS {name} (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    username        TEXT,
    name            TEXT    NOT NULL DEFAULT '',
    phone           TEXT    NOT NULL DEFAULT '',
    service         TEXT    NOT NULL DEFAULT '',
    price           TEXT,
    model           TEXT    NOT NULL DEFAULT '',
    day             TEXT    NOT NULL,
    time            TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',
    reminded_day    INTEGER NOT NULL DEFAULT 0,
    reminded_soon   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT ''
)
"""

TABLE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    created_at  TEXT NOT NULL DEFAULT '',
    seen_at     TEXT NOT NULL DEFAULT ''
)
"""

INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_bookings_active_slot
    ON bookings(day, time) WHERE status IN ('pending', 'confirmed');
CREATE INDEX IF NOT EXISTS ix_bookings_day ON bookings(day, status);
CREATE INDEX IF NOT EXISTS ix_bookings_user ON bookings(user_id, status);
CREATE INDEX IF NOT EXISTS ix_bookings_phone ON bookings(phone);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # меньше блокировок при параллельных запросах
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def db():
    """Соединение, которое всегда закрывается (в старом коде утекали дескрипторы)."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _rebuild_legacy_table(conn: sqlite3.Connection) -> None:
    """Старая схема содержала UNIQUE(day, time) — из-за него нельзя было
    отменить запись и занять слот снова. Пересобираем таблицу без него."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bookings'"
    ).fetchone()
    if not row or not row["sql"] or "UNIQUE" not in row["sql"].upper():
        return

    old_cols = {r["name"] for r in conn.execute("PRAGMA table_info(bookings)")}
    conn.execute("ALTER TABLE bookings RENAME TO bookings_legacy")
    conn.execute(TABLE_BOOKINGS.format(name="bookings"))

    new_cols = [r["name"] for r in conn.execute("PRAGMA table_info(bookings)")]
    source: dict[str, str] = {}
    for col in new_cols:
        if col in old_cols:
            source[col] = col
        elif col == "reminded_day" and "reminded" in old_cols:
            source[col] = "reminded"

    targets = list(source)
    conn.execute(
        f"INSERT INTO bookings ({', '.join(targets)}) "
        f"SELECT {', '.join(source[c] for c in targets)} FROM bookings_legacy"
    )
    conn.execute("DROP TABLE bookings_legacy")
    logging.info("Миграция bookings выполнена: старый UNIQUE(day, time) снят")


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for column, ddl in columns.items():
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    with db() as conn:
        _rebuild_legacy_table(conn)
        conn.execute(TABLE_BOOKINGS.format(name="bookings"))
        conn.execute(TABLE_USERS)
        _add_columns(conn, "bookings", {
            "username": "TEXT",
            "price": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "reminded_day": "INTEGER NOT NULL DEFAULT 0",
            "reminded_soon": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "TEXT NOT NULL DEFAULT ''",
        })
        conn.executescript(INDEXES)


def busy_slots(day: str) -> set[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT time FROM bookings WHERE day=? AND status IN ('pending','confirmed')",
            (day,),
        )
        return {r["time"] for r in rows}


def free_slots(day: str, exclude: sqlite3.Row | None = None) -> list[str]:
    """Свободные слоты дня с учётом занятости и минимального запаса времени."""
    if not is_bookable_day(day):
        return []
    busy = busy_slots(day)
    if exclude is not None and exclude["day"] == day:
        busy.discard(exclude["time"])
    deadline = now() + timedelta(minutes=LEAD_MINUTES)
    return [t for t in slot_times() if t not in busy and slot_dt(day, t) > deadline]


def upsert_user(user) -> None:
    if user is None:
        return
    with db() as conn:
        conn.execute(
            """INSERT INTO users (user_id, username, first_name, created_at, seen_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username=excluded.username,
                   first_name=excluded.first_name,
                   seen_at=excluded.seen_at""",
            (user.id, user.username, user.first_name, iso_now(), iso_now()),
        )


def active_count(user_id: int) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM bookings "
            "WHERE user_id=? AND day>=? AND status IN ('pending','confirmed')",
            (user_id, today_str()),
        ).fetchone()
        return int(row[0])


def active_bookings(user_id: int) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM bookings "
            "WHERE user_id=? AND day>=? AND status IN ('pending','confirmed') "
            "ORDER BY day, time",
            (user_id, today_str()),
        ).fetchall()


def next_booking(user_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM bookings "
            "WHERE user_id=? AND day>=? AND status IN ('pending','confirmed') "
            "ORDER BY day, time LIMIT 1",
            (user_id, today_str()),
        ).fetchone()


def get_booking(booking_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()


def bookings_for_day(day: str) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM bookings WHERE day=? AND status!='cancelled' ORDER BY time",
            (day,),
        ).fetchall()


def all_user_ids() -> list[int]:
    with db() as conn:
        return [r["user_id"] for r in conn.execute("SELECT user_id FROM users")]


# ============================== КЛАВИАТУРЫ ==============================

def menu_kb(user_id: int | None = None) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text=BTN_BOOK)],
        [KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_PRICES)],
        [KeyboardButton(text=BTN_CONTACTS), KeyboardButton(text=BTN_HELP)],
    ]
    if ADMIN_ID and user_id == ADMIN_ID:
        keyboard.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def services_kb():
    builder = InlineKeyboardBuilder()
    for i, (name, price, _term) in enumerate(SERVICES):
        builder.button(text=f"{name} · {price}", callback_data=f"svc:{i}")
    builder.adjust(1)
    return builder.as_markup()


def days_kb(prefix: str = "day", back_cb: str | None = "back:svc",
            exclude: sqlite3.Row | None = None):
    """Дни, в которых остались свободные слоты. None — если свободных дней нет."""
    builder = InlineKeyboardBuilder()
    today = now().date()
    added = 0
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        if d.weekday() in DAYS_OFF:
            continue
        day_iso = d.isoformat()
        if not free_slots(day_iso, exclude=exclude):
            continue
        if i == 0:
            label = "Сегодня"
        elif i == 1:
            label = "Завтра"
        else:
            label = f"{d:%d.%m} {WEEKDAYS[d.weekday()]}"
        builder.button(text=label, callback_data=f"{prefix}:{day_iso}")
        added += 1
    if not added:
        return None
    builder.adjust(3)
    if back_cb:
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb))
    return builder.as_markup()


def times_kb(day: str, prefix: str = "time", back_cb: str = "back:day",
             exclude: sqlite3.Row | None = None):
    builder = InlineKeyboardBuilder()
    slots = free_slots(day, exclude=exclude)
    for t in slots:
        builder.button(text=t, callback_data=f"{prefix}:{t}")
    if slots:
        builder.adjust(3)
    builder.row(InlineKeyboardButton(text="⬅️ Другой день", callback_data=back_cb))
    return builder.as_markup(), len(slots)


def confirm_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить запись", callback_data="confirm")
    builder.button(text="❌ Отмена", callback_data="abort")
    builder.adjust(1)
    return builder.as_markup()


def my_kb(rows: list[sqlite3.Row]):
    builder = InlineKeyboardBuilder()
    for row in rows:
        short_day = f"{row['day'][8:]}.{row['day'][5:7]}"
        builder.row(
            InlineKeyboardButton(
                text=f"🔁 {short_day} {row['time']}",
                callback_data=f"move:{row['id']}"),
            InlineKeyboardButton(
                text="❌ Отменить",
                callback_data=f"cancel:{row['id']}"),
        )
    builder.row(InlineKeyboardButton(text="📝 Новая запись", callback_data="new:booking"))
    return builder.as_markup()


def admin_menu_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Сегодня", callback_data="adm:today")
    builder.button(text="📆 Завтра", callback_data="adm:tomorrow")
    builder.button(text="🗓 7 дней", callback_data="adm:week")
    builder.button(text="📊 Статистика", callback_data="adm:stats")
    builder.button(text="📤 CSV", callback_data="adm:export")
    builder.button(text="📣 Рассылка", callback_data="adm:broadcast")
    builder.adjust(3, 3)
    return builder.as_markup()


# ============================== СОСТОЯНИЯ ==============================

class Form(StatesGroup):
    model = State()
    name = State()
    phone = State()


class AdminForm(StatesGroup):
    broadcast = State()


# ============================== ВСПОМОГАТЕЛЬНОЕ ==============================

def cb_int(data: str | None, index: int = 1) -> int | None:
    try:
        return int((data or "").split(":")[index])
    except (IndexError, ValueError):
        return None


async def notify(bot: Bot, chat_id: int, text: str, markup=None, attempt: int = 0) -> bool:
    """Отправка с защитой от блокировок, флуд-лимитов и битого HTML."""
    try:
        await bot.send_message(
            chat_id, text, reply_markup=markup,
            parse_mode=ParseMode.HTML, link_preview_is_disabled=True,
        )
        return True
    except TelegramForbiddenError:
        return True  # пользователь заблокировал бота — повторять бессмысленно
    except TelegramRetryAfter as e:
        if attempt >= 2:
            return False
        await asyncio.sleep(min(e.retry_after, 20))
        return await notify(bot, chat_id, text, markup, attempt + 1)
    except TelegramBadRequest as e:
        logging.warning("HTML отклонён для %s: %s", chat_id, e)
        try:
            await bot.send_message(
                chat_id, re.sub(r"<[^>]+>", "", text),
                reply_markup=markup, parse_mode=None,
            )
            return True
        except TelegramAPIError:
            return False
    except TelegramAPIError:
        logging.exception("Не удалось отправить сообщение %s", chat_id)
        return False


async def send_plain(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id, text, parse_mode=None)
        return True
    except TelegramForbiddenError:
        return False
    except TelegramRetryAfter as e:
        await asyncio.sleep(min(e.retry_after, 20))
        try:
            await bot.send_message(chat_id, text, parse_mode=None)
            return True
        except TelegramAPIError:
            return False
    except TelegramAPIError:
        logging.warning("Рассылка не доставлена %s", chat_id)
        return False


def my_text(user_id: int) -> tuple[str, object]:
    rows = active_bookings(user_id)
    if not rows:
        return ("У вас нет активных записей.\n\n"
                "Нажмите «📝 Записаться», чтобы выбрать время."), None
    lines = ["<b>Ваши записи</b>"]
    for row in rows:
        mark = "✅" if row["status"] == "confirmed" else "⏳"
        lines.append(
            f"{mark} <b>{fmt_day(row['day'])}, {row['time']}</b>\n"
            f"   {esc(row['service'])}, {esc(row['model'])}"
        )
    lines.append("\nПеренести или отменить — кнопками ниже.")
    return "\n".join(lines), my_kb(rows)


async def send_my(message: Message, note: str = "") -> None:
    text, markup = my_text(message.from_user.id)
    if note:
        text = f"{note}\n\n{text}"
    await message.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def edit_my(call: CallbackQuery, note: str = "") -> None:
    text, markup = my_text(call.from_user.id)
    if note:
        text = f"{note}\n\n{text}"
    try:
        await call.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass


def summary_text(data: dict) -> str:
    return (
        "<b>Проверьте заявку</b>\n\n"
        f"🔧 {esc(data['service'])} — {esc(data.get('price', ''))}\n"
        f"📱 {esc(data['model'])}\n"
        f"🗓 {fmt_day(data['day'])}, {data['time']}\n"
        f"👤 {esc(data['name'])}\n"
        f"📞 {esc(fmt_phone(data['phone']))}"
    )


def admin_booking_text(booking_id: int, data: dict, username: str | None,
                       booking_day: str | None = None,
                       booking_time: str | None = None) -> str:
    text = (
        f"🆕 <b>Заявка #{booking_id}</b>\n\n"
        f"🗓 {fmt_day(booking_day or data['day'])} в {booking_time or data['time']}\n"
        f"🔧 {esc(data['service'])} ({esc(data.get('price', ''))})\n"
        f"📱 {esc(data['model'])}\n"
        f"👤 {esc(data['name'])}\n"
        f"📞 {esc(fmt_phone(data['phone']))}"
    )
    if username:
        text += f"\n🔗 @{esc(username)}"
    return text


def admin_booking_kb(booking_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=f"adm_ok:{booking_id}")
    builder.button(text="❌ Отклонить", callback_data=f"adm_no:{booking_id}")
    builder.adjust(2)
    return builder.as_markup()


# ============================== СТАРТ И МЕНЮ ==============================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    upsert_user(message.from_user)
    text = (f"Здравствуйте! Это <b>{esc(SHOP_NAME)}</b>, ремонт телефонов.\n\n"
            f"📍 {esc(ADDRESS)}\n🕒 {esc(WORK_TEXT)}\n\n"
            "Выберите действие в меню внизу.")
    booking = next_booking(message.from_user.id)
    if booking:
        text += (f"\n\nВаша ближайшая запись: <b>{fmt_day(booking['day'])}, "
                 f"{booking['time']}</b>")
    await message.answer(text, reply_markup=menu_kb(message.from_user.id),
                         parse_mode=ParseMode.HTML)


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message):
    await message.answer(
        "<b>Как записаться</b>\n"
        "1. Нажмите «📝 Записаться» и выберите услугу.\n"
        "2. Напишите модель телефона.\n"
        "3. Выберите день и время.\n"
        "4. Оставьте имя и телефон.\n"
        "5. Проверьте заявку и нажмите «Подтвердить».\n\n"
        "Заявка сразу уходит мастеру, он подтвердит запись сообщением.\n\n"
        "Отменить или перенести визит можно в «📋 Мои записи».\n"
        f"Вопросы: {esc(PHONE)} или {esc(TELEGRAM_URL)}",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Хорошо, отменил. Что дальше?",
                         reply_markup=menu_kb(message.from_user.id))


@router.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>",
                         parse_mode=ParseMode.HTML)


@router.message(F.text == BTN_BOOK)
async def book(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Что нужно отремонтировать?", reply_markup=services_kb())


@router.message(F.text == BTN_PRICES)
async def prices(message: Message):
    lines = [f"• {esc(n)}: <b>{esc(p)}</b>, {esc(t)}" for n, p, t in SERVICES]
    await message.answer(
        "<b>Ориентировочные цены</b>\n\n" + "\n".join(lines) +
        "\n\nТочную стоимость назовём после бесплатной диагностики.",
        parse_mode=ParseMode.HTML,
    )


@router.message(F.text == BTN_CONTACTS)
async def contacts(message: Message):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💬 Написать в Telegram", url=TELEGRAM_URL))
    if MAPS_URL:
        builder.row(InlineKeyboardButton(text="🗺 Открыть на карте", url=MAPS_URL))
    await message.answer(
        f"<b>{esc(SHOP_NAME)}</b>\n\n"
        f"📍 {esc(ADDRESS)}\n"
        f"📞 {esc(PHONE)}\n"
        f"🕒 {esc(WORK_TEXT)}",
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("my"))
@router.message(F.text == BTN_MY)
async def my_bookings(message: Message):
    await send_my(message)


# ============================== ЗАПИСЬ ==============================

@router.callback_query(F.data.startswith("svc:"))
async def pick_service(call: CallbackQuery, state: FSMContext):
    index = cb_int(call.data)
    if index is None or not 0 <= index < len(SERVICES):
        await call.message.edit_text("Список услуг обновился, выберите заново:",
                                     reply_markup=services_kb())
        return await call.answer()
    name, price, term = SERVICES[index]
    await state.clear()
    await state.update_data(service=name, price=price, term=term)
    await state.set_state(Form.model)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ Другая услуга", callback_data="back:svc"))
    await call.message.edit_text(
        f"Услуга: <b>{esc(name)}</b>\nЦена: {esc(price)}, срок: {esc(term)}\n\n"
        "Напишите модель телефона (например, iPhone 12 или Redmi Note 11):",
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.HTML,
    )
    await call.answer()


@router.callback_query(F.data == "back:svc")
async def back_to_services(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Что нужно отремонтировать?", reply_markup=services_kb())
    await call.answer()


@router.callback_query(F.data == "back:model")
async def back_to_model(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    data.pop("day", None)
    data.pop("time", None)
    await state.set_data(data)
    await state.set_state(Form.model)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ Другая услуга", callback_data="back:svc"))
    await call.message.edit_text(
        "Напишите модель телефона ещё раз:",
        reply_markup=builder.as_markup(),
    )
    await call.answer()


@router.message(Form.model, F.text)
async def get_model(message: Message, state: FSMContext):
    model = clean_model(message.text)
    if model is None:
        return await message.answer(
            "Модель не распознал. Напишите коротко текстом, например: iPhone 12.")
    data = await state.get_data()
    if "service" not in data:
        await state.clear()
        return await message.answer("Начнём заново: нажмите «📝 Записаться».")
    await state.update_data(model=model)
    await state.set_state(None)   # дальше идём по кнопкам, а не по тексту
    keyboard = days_kb("day", back_cb="back:model")
    if keyboard is None:
        return await message.answer(
            "Свободных слотов на ближайшую неделю нет. "
            f"Позвоните нам: {esc(PHONE)}", parse_mode=ParseMode.HTML)
    await message.answer(
        f"Услуга: <b>{esc(data['service'])}</b>, телефон: {esc(model)}\n\nВыберите день:",
        reply_markup=keyboard, parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("day:"))
async def pick_day(call: CallbackQuery, state: FSMContext):
    day = (call.data or "").split(":", 1)[-1]
    if not is_bookable_day(day):
        await call.answer("Этот день недоступен", show_alert=True)
        keyboard = days_kb("day", back_cb="back:model")
        if keyboard:
            await call.message.edit_reply_markup(reply_markup=keyboard)
        return
    keyboard, count = times_kb(day, "time", back_cb="back:day")
    if count == 0:
        await call.answer("На этот день свободного времени уже нет", show_alert=True)
        return
    data = await state.get_data()
    await state.update_data(day=day)
    text = f"Шаг 3. {fmt_day(day)}, выберите время:"
    if data.get("service"):
        text = f"{esc(data['service'])}, {esc(data.get('model', ''))}\n\n" + text
    await call.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    await call.answer()


@router.callback_query(F.data == "back:day")
async def back_to_day(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    keyboard = days_kb("day", back_cb="back:model")
    if keyboard is None:
        await call.answer("Свободных дней не осталось", show_alert=True)
        return
    await call.message.edit_text("Выберите день:", reply_markup=keyboard)
    await call.answer()


@router.callback_query(F.data.startswith("time:"))
async def pick_time(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("day") or not data.get("service"):
        await call.message.answer("Давайте начнём заново: нажмите «📝 Записаться».")
        return await call.answer()
    slot = (call.data or "").split(":", 1)[-1]
    if slot not in slot_times() or slot not in free_slots(data["day"]):
        await call.answer("Это время только что заняли, выберите другое", show_alert=True)
        await call.message.edit_reply_markup(reply_markup=times_kb(data["day"])[0])
        return
    await state.update_data(time=slot)
    await state.set_state(Form.name)
    await call.message.edit_text(
        f"Выбрано: <b>{fmt_day(data['day'])}, {slot}</b>\n\nКак вас зовут?",
        parse_mode=ParseMode.HTML)
    await call.answer()


@router.message(Form.name, F.text)
async def get_name(message: Message, state: FSMContext):
    name = clean_name(message.text)
    if name is None:
        return await message.answer("Имя должно быть текстом, 2-50 символов. Напишите ещё раз:")
    await state.update_data(name=name)
    await state.set_state(Form.phone)
    await message.answer("Отправьте номер телефона кнопкой ниже или напишите вручную:",
                         reply_markup=phone_kb())


@router.message(Form.phone, F.contact | F.text)
async def get_phone(message: Message, state: FSMContext):
    contact = message.contact
    if contact is not None:
        if contact.user_id and contact.user_id != message.from_user.id:
            return await message.answer("Это чужой контакт. Отправьте свой номер.")
        raw = contact.phone_number
    else:
        raw = message.text or ""
    phone = normalize_phone(raw)
    if phone is None:
        return await message.answer(
            "Не похоже на номер. Напишите в формате +7 900 000-00-00 или отправьте кнопкой.",
            reply_markup=phone_kb())
    await state.update_data(phone=phone)
    await state.set_state(None)
    data = await state.get_data()
    await message.answer("Спасибо! Проверьте заявку:",
                         reply_markup=menu_kb(message.from_user.id))
    await message.answer(summary_text(data), reply_markup=confirm_kb(),
                         parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "abort")
async def abort(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("Заявка отменена. Чтобы начать заново, нажмите «📝 Записаться».")
    except TelegramBadRequest:
        pass
    await call.answer()


@router.callback_query(F.data == "new:booking")
async def new_booking(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Что нужно отремонтировать?", reply_markup=services_kb())
    await call.answer()


@router.callback_query(F.data == "confirm")
async def confirm(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    required = ("service", "model", "day", "time", "name", "phone")
    if any(not data.get(key) for key in required):
        await state.clear()
        await call.message.edit_text("Данные заявки потерялись. Начните заново: «📝 Записаться».")
        return await call.answer()

    day, slot = data["day"], data["time"]
    if not is_bookable_day(day) or slot not in slot_times():
        await state.clear()
        await call.message.edit_text("Это время уже недоступно. Выберите другое: «📝 Записаться».")
        return await call.answer()
    if slot_dt(day, slot) <= now() + timedelta(minutes=LEAD_MINUTES):
        await state.clear()
        await call.message.edit_text("Это время уже прошло. Выберите другое: «📝 Записаться».")
        return await call.answer()
    if active_count(call.from_user.id) >= MAX_ACTIVE:
        await state.clear()
        await call.message.edit_text(
            f"У вас уже {MAX_ACTIVE} активных записи. Отмените одну в «📋 Мои записи» "
            "или позвоните нам.")
        return await call.answer()

    try:
        with db() as conn:
            cursor = conn.execute(
                """INSERT INTO bookings
                   (user_id, username, name, phone, service, price, model, day, time,
                    status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (call.from_user.id, call.from_user.username, data["name"], data["phone"],
                 data["service"], data.get("price"), data["model"], day, slot, iso_now()),
            )
            booking_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError:
        await state.clear()
        await call.message.edit_text(
            "Это время только что заняли. Нажмите «📝 Записаться» и выберите другое.")
        return await call.answer()

    await state.clear()
    try:
        await call.message.edit_text(
            "✅ <b>Заявка принята!</b>\n\n"
            f"🗓 {fmt_day(day)} в {slot}\n"
            f"🔧 {esc(data['service'])}, {esc(data['model'])}\n\n"
            f"📍 {esc(ADDRESS)}\n📞 {esc(PHONE)}\n\n"
            "Скоро подтвердим запись — пришлём сообщение.\n"
            "Отменить или перенести можно в «📋 Мои записи».",
            parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    await call.answer("Заявка отправлена")

    if ADMIN_ID:
        text = admin_booking_text(booking_id, data, call.from_user.username)
        await notify(bot, ADMIN_ID, text, markup=admin_booking_kb(booking_id))


# ============================== МОИ ЗАПИСИ: ОТМЕНА И ПЕРЕНОС ==============================

@router.callback_query(F.data.startswith("cancel:"))
async def cancel_booking(call: CallbackQuery, state: FSMContext, bot: Bot):
    booking_id = cb_int(call.data)
    row = get_booking(booking_id) if booking_id else None
    if row is None or row["user_id"] != call.from_user.id:
        await call.answer("Запись не найдена", show_alert=True)
        return
    if row["status"] not in ACTIVE_STATUSES:
        await call.answer("Эта запись уже не активна", show_alert=True)
        return
    with db() as conn:
        conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (row["id"],))
    await call.answer("Запись отменена")
    await edit_my(call, note=f"Запись на {fmt_day(row['day'])}, {row['time']} отменена.")
    if ADMIN_ID:
        await notify(bot, ADMIN_ID,
                     f"❌ Клиент отменил запись #{row['id']}: "
                     f"{fmt_day(row['day'])} {row['time']}, {esc(row['name'])}, "
                     f"{esc(fmt_phone(row['phone']))}")


@router.callback_query(F.data.startswith("move:"))
async def move_start(call: CallbackQuery, state: FSMContext):
    booking_id = cb_int(call.data)
    row = get_booking(booking_id) if booking_id else None
    if row is None or row["user_id"] != call.from_user.id:
        await call.answer("Запись не найдена", show_alert=True)
        return
    if row["status"] not in ACTIVE_STATUSES:
        await call.answer("Эта запись уже не активна", show_alert=True)
        return
    await state.update_data(move_id=row["id"])
    keyboard = days_kb("mday", back_cb="back:my", exclude=row)
    if keyboard is None:
        await call.answer("Свободных дней нет, позвоните нам", show_alert=True)
        return
    await call.message.edit_text(
        f"Перенос записи с {fmt_day(row['day'])}, {row['time']}.\n\nВыберите новый день:",
        reply_markup=keyboard)
    await call.answer()


@router.callback_query(F.data.startswith("mday:"))
async def move_day(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    move_id = data.get("move_id")
    row = get_booking(int(move_id)) if move_id else None
    if row is None or row["user_id"] != call.from_user.id:
        await call.answer("Начните перенос заново", show_alert=True)
        return
    day = (call.data or "").split(":", 1)[-1]
    if not is_bookable_day(day):
        await call.answer("Этот день недоступен", show_alert=True)
        return
    keyboard, count = times_kb(day, "mtime", back_cb="mback:day", exclude=row)
    if count == 0:
        await call.answer("На этот день свободного времени нет", show_alert=True)
        return
    await state.update_data(move_day=day)
    await call.message.edit_text(f"{fmt_day(day)}, выберите время:", reply_markup=keyboard)
    await call.answer()


@router.callback_query(F.data == "mback:day")
async def move_back_day(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    move_id = data.get("move_id")
    row = get_booking(int(move_id)) if move_id else None
    keyboard = days_kb("mday", back_cb="back:my", exclude=row)
    if keyboard is None:
        await call.answer("Свободных дней нет", show_alert=True)
        return
    await call.message.edit_text("Выберите новый день:", reply_markup=keyboard)
    await call.answer()


@router.callback_query(F.data.startswith("mtime:"))
async def move_time(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    move_id = data.get("move_id")
    row = get_booking(int(move_id)) if move_id else None
    if row is None or row["user_id"] != call.from_user.id:
        await call.answer("Начните перенос заново", show_alert=True)
        return
    if "move_day" not in data or not data["move_day"]:
        await call.answer("Выберите день заново", show_alert=True)
        return
    day = data["move_day"]
    slot = (call.data or "").split(":", 1)[-1]
    if slot not in slot_times() or slot not in free_slots(day, exclude=row):
        await call.answer("Это время только что заняли", show_alert=True)
        await call.message.edit_reply_markup(
            reply_markup=times_kb(day, "mtime", back_cb="mback:day", exclude=row)[0])
        return
    try:
        with db() as conn:
            conn.execute(
                """UPDATE bookings
                   SET day=?, time=?, status='pending', reminded_day=0, reminded_soon=0
                   WHERE id=? AND user_id=?""",
                (day, slot, row["id"], call.from_user.id),
            )
    except sqlite3.IntegrityError:
        await call.answer("Это время только что заняли", show_alert=True)
        return
    await state.update_data(move_id=None, move_day=None)
    await call.answer("Запись перенесена")
    await edit_my(call, note=(f"Запись перенесена: {fmt_day(day)}, {slot}.\n"
                              "Мастер подтвердит новое время."))
    if ADMIN_ID:
        await notify(bot, ADMIN_ID,
                     f"🔁 Клиент перенёс запись #{row['id']}: "
                     f"{fmt_day(row['day'])} {row['time']} → {fmt_day(day)} {slot}\n"
                     f"{esc(row['name'])}, {esc(fmt_phone(row['phone']))}")


@router.callback_query(F.data == "back:my")
async def back_to_my(call: CallbackQuery, state: FSMContext):
    await state.update_data(move_id=None, move_day=None)
    await edit_my(call)
    await call.answer()


# ============================== ДЛЯ ВЛАДЕЛЬЦА ==============================

@router.message(F.text == BTN_ADMIN)
async def admin_button(message: Message):
    if not is_admin(message.from_user):
        return
    await message.answer("Админка:", reply_markup=admin_menu_kb())


@router.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user):
        return
    await message.answer("Админка:", reply_markup=admin_menu_kb())


@router.callback_query(F.data.startswith("adm_"))
async def admin_action(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user):
        return await call.answer("Недостаточно прав", show_alert=True)
    action, _, raw_id = (call.data or "").partition(":")
    booking_id = cb_int(call.data)
    if booking_id is None:
        return await call.answer("Некорректная кнопка", show_alert=True)
    row = get_booking(booking_id)
    if row is None:
        await call.answer("Запись не найдена", show_alert=True)
        return
    if row["status"] not in ACTIVE_STATUSES:
        await call.answer("Запись уже обработана", show_alert=True)
        return

    if action == "adm_ok":
        with db() as conn:
            conn.execute("UPDATE bookings SET status='confirmed' WHERE id=?", (row["id"],))
        await notify(bot, row["user_id"],
                     f"✅ Ваша запись на {fmt_day(row['day'])}, {row['time']} подтверждена.\n"
                     f"Ждём вас: {esc(ADDRESS)}")
        note = "✅ Подтверждено"
    else:
        with db() as conn:
            conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (row["id"],))
        await notify(bot, row["user_id"],
                     f"К сожалению, время {fmt_day(row['day'])}, {row['time']} не подходит.\n"
                     "Выберите другое в «📝 Записаться» или позвоните нам: "
                     f"{esc(PHONE)}")
        note = "❌ Отклонено"

    try:
        source = call.message.text if call.message and call.message.text else ""
        await call.message.edit_text(
            esc(source) + f"\n\n{note}", parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    await call.answer(note)


@router.callback_query(F.data.startswith("adm:"))
async def admin_menu_action(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(call.from_user):
        return await call.answer("Недостаточно прав", show_alert=True)
    action = (call.data or "").split(":", 1)[-1]
    if action == "today":
        await call.message.edit_text(day_report(today_str()), reply_markup=admin_menu_kb(),
                                     parse_mode=ParseMode.HTML)
    elif action == "tomorrow":
        tomorrow = (now().date() + timedelta(days=1)).isoformat()
        await call.message.edit_text(day_report(tomorrow), reply_markup=admin_menu_kb(),
                                     parse_mode=ParseMode.HTML)
    elif action == "week":
        await call.message.edit_text(week_report(), reply_markup=admin_menu_kb(),
                                     parse_mode=ParseMode.HTML)
    elif action == "stats":
        await call.message.edit_text(stats_report(), reply_markup=admin_menu_kb(),
                                     parse_mode=ParseMode.HTML)
    elif action == "export":
        await send_export(call.message)
    elif action == "broadcast":
        await state.set_state(AdminForm.broadcast)
        await call.message.answer(
            "Отправьте текст рассылки одним сообщением. /cancel — отмена.")
    await call.answer()


def day_report(day: str) -> str:
    rows = bookings_for_day(day)
    title = f"<b>{fmt_day(day)}</b>"
    if not rows:
        return f"{title}\nЗаписей нет."
    marks = {"confirmed": "✅", "pending": "⏳", "done": "🏁"}
    lines = [f"{title} — {len(rows)} запис."]
    for row in rows:
        mark = marks.get(row["status"], "•")
        lines.append(
            f"{mark} <b>{row['time']}</b> — {esc(row['name'])}, {esc(fmt_phone(row['phone']))}\n"
            f"    {esc(row['service'])}, {esc(row['model'])}"
        )
    return "\n".join(lines)


def week_report() -> str:
    lines = ["<b>Ближайшие дни</b>"]
    total = 0
    for i in range(DAYS_AHEAD):
        d = now().date() + timedelta(days=i)
        if d.weekday() in DAYS_OFF:
            continue
        rows = bookings_for_day(d.isoformat())
        total += len(rows)
        lines.append(f"{fmt_day(d.isoformat())}: {len(rows)}")
    lines.append(f"\nВсего за период: {total}")
    return "\n".join(lines)


def stats_report() -> str:
    today = today_str()
    with db() as conn:
        def scalar(sql: str, params: tuple = ()) -> int:
            return int(conn.execute(sql, params).fetchone()[0])

        pending = scalar("SELECT COUNT(*) FROM bookings WHERE status='pending' AND day>=?", (today,))
        confirmed = scalar("SELECT COUNT(*) FROM bookings WHERE status='confirmed' AND day>=?", (today,))
        today_count = scalar("SELECT COUNT(*) FROM bookings WHERE day=? AND status!='cancelled'", (today,))
        users = scalar("SELECT COUNT(*) FROM users")
        all_time = scalar("SELECT COUNT(*) FROM bookings")
    return (
        "<b>Статистика</b>\n\n"
        f"Сегодня записей: {today_count}\n"
        f"Ожидают подтверждения: {pending}\n"
        f"Подтверждено: {confirmed}\n"
        f"Пользователей в боте: {users}\n"
        f"Всего заявок за всё время: {all_time}"
    )


def find_bookings(query: str) -> list[sqlite3.Row]:
    text = query.strip()
    digits = re.sub(r"\D", "", text)
    with db() as conn:
        if len(digits) >= 4:
            return conn.execute(
                "SELECT * FROM bookings WHERE phone LIKE ? OR name LIKE ? OR model LIKE ? "
                "ORDER BY day DESC, time DESC LIMIT 20",
                (f"%{digits}%", f"%{text}%", f"%{text}%"),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM bookings WHERE name LIKE ? OR model LIKE ? "
            "ORDER BY day DESC, time DESC LIMIT 20",
            (f"%{text}%", f"%{text}%"),
        ).fetchall()


@router.message(Command("today"))
async def cmd_today(message: Message):
    if not is_admin(message.from_user):
        return
    await message.answer(day_report(today_str()), parse_mode=ParseMode.HTML)


@router.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message):
    if not is_admin(message.from_user):
        return
    tomorrow = (now().date() + timedelta(days=1)).isoformat()
    await message.answer(day_report(tomorrow), parse_mode=ParseMode.HTML)


@router.message(Command("week"))
async def cmd_week(message: Message):
    if not is_admin(message.from_user):
        return
    await message.answer(week_report(), parse_mode=ParseMode.HTML)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user):
        return
    await message.answer(stats_report(), parse_mode=ParseMode.HTML)


@router.message(Command("find"))
async def cmd_find(message: Message):
    if not is_admin(message.from_user):
        return
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        return await message.answer("Использование: <code>/find +79001234567</code>\n"
                                    "Искать можно по телефону, имени или модели.",
                                    parse_mode=ParseMode.HTML)
    rows = find_bookings(query)
    if not rows:
        return await message.answer("Ничего не найдено.")
    marks = {"confirmed": "✅", "pending": "⏳", "cancelled": "❌", "done": "🏁"}
    lines = [f"Найдено: {len(rows)}"]
    for row in rows:
        mark = marks.get(row["status"], "•")
        lines.append(
            f"{mark} #{row['id']} {fmt_day(row['day'])} {row['time']}\n"
            f"    {esc(row['name'])}, {esc(fmt_phone(row['phone']))}\n"
            f"    {esc(row['service'])}, {esc(row['model'])}"
        )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


async def send_export(message: Message) -> None:
    today = today_str()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE day>=? ORDER BY day, time", (today,)
        ).fetchall()
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["id", "date", "time", "status", "service", "model",
                     "name", "phone", "username", "created"])
    for row in rows:
        writer.writerow([
            row["id"], row["day"], row["time"], row["status"], row["service"],
            row["model"], row["name"], row["phone"], row["username"], row["created_at"],
        ])
    payload = buffer.getvalue().encode("utf-8-sig")
    await message.answer_document(
        BufferedInputFile(payload, filename=f"bookings_{today}.csv"),
        caption=f"Записи с {today}: {len(rows)}",
    )


@router.message(Command("export"))
async def cmd_export(message: Message):
    if not is_admin(message.from_user):
        return
    await send_export(message)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    await state.set_state(AdminForm.broadcast)
    await message.answer("Отправьте текст рассылки одним сообщением. /cancel — отмена.")


@router.message(AdminForm.broadcast, F.text)
async def do_broadcast(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        return await message.answer("Нужен текст сообщения.")
    await state.clear()
    user_ids = all_user_ids()
    status = await message.answer(f"Рассылаю {len(user_ids)} пользователям…")
    delivered = failed = 0
    for user_id in user_ids:
        if await send_plain(bot, user_id, text):
            delivered += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"Готово: доставлено {delivered}, не доставлено {failed}.")


# ============================== НАПОМИНАНИЯ ==============================

async def send_reminders(bot: Bot) -> None:
    current = now()

    # 1. Вечернее напоминание на завтра
    if current.hour >= REMIND_EVENING_HOUR:
        tomorrow = (current.date() + timedelta(days=1)).isoformat()
        with db() as conn:
            rows = conn.execute(
                "SELECT id, user_id, time, service, model FROM bookings "
                "WHERE day=? AND status IN ('pending','confirmed') AND reminded_day=0",
                (tomorrow,),
            ).fetchall()
        for row in rows:
            text = (
                "🔔 <b>Напоминание о записи</b>\n\n"
                f"Завтра в <b>{row['time']}</b> ждём вас.\n"
                f"🔧 {esc(row['service'])}, {esc(row['model'])}\n"
                f"📍 {esc(ADDRESS)}\n📞 {esc(PHONE)}\n\n"
                "Если планы изменились — отмените запись в «📋 Мои записи»."
            )
            if await notify(bot, row["user_id"], text):
                with db() as conn:
                    conn.execute("UPDATE bookings SET reminded_day=1 WHERE id=?", (row["id"],))
            await asyncio.sleep(0.05)

    # 2. Напоминание за REMIND_SOON_HOURS часов до визита
    with db() as conn:
        rows = conn.execute(
            "SELECT id, user_id, day, time, service, model FROM bookings "
            "WHERE day=? AND status IN ('pending','confirmed') AND reminded_soon=0",
            (current.date().isoformat(),),
        ).fetchall()
    for row in rows:
        left = slot_dt(row["day"], row["time"]) - current
        if not timedelta(0) < left <= timedelta(hours=REMIND_SOON_HOURS):
            continue
        hours = max(1, round(left.total_seconds() / 3600))
        text = (
            "⏰ <b>Скоро ждём вас</b>\n\n"
            f"Сегодня в <b>{row['time']}</b> (~{hours} ч).\n"
            f"🔧 {esc(row['service'])}, {esc(row['model'])}\n"
            f"📍 {esc(ADDRESS)}"
        )
        if await notify(bot, row["user_id"], text):
            with db() as conn:
                conn.execute("UPDATE bookings SET reminded_soon=1 WHERE id=?", (row["id"],))
        await asyncio.sleep(0.05)


async def reminder_loop(bot: Bot) -> None:
    while True:
        try:
            await send_reminders(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Сбой в цикле напоминаний")
        await asyncio.sleep(REMINDER_TICK)


# ============================== ЗАПАСНОЙ ОБРАБОТЧИК ==============================

@router.message()
async def fallback(message: Message, state: FSMContext):
    current = await state.get_state()
    if current:
        await message.answer(
            "Не понял. Напишите текстом или нажмите /cancel, чтобы начать заново.",
            reply_markup=menu_kb(message.from_user.id))
    else:
        await message.answer(
            "Не понял команду. Выберите пункт меню внизу или нажмите /help.",
            reply_markup=menu_kb(message.from_user.id))


# ============================== ЗАПУСК ==============================

def setup_logging() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)
    try:
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        logging.warning("Не удалось открыть файл логов %s", LOG_PATH)


def make_bot() -> Bot:
    try:
        from aiogram.client.default import DefaultBotProperties

        return Bot(BOT_TOKEN, default=DefaultBotProperties(
            parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    except ImportError:  # aiogram < 3.7
        return Bot(BOT_TOKEN, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def set_commands(bot: Bot) -> None:
    user_commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="my", description="Мои записи"),
        BotCommand(command="cancel", description="Отменить ввод"),
        BotCommand(command="help", description="Как записаться"),
        BotCommand(command="id", description="Мой Telegram ID"),
    ]
    try:
        await bot.set_my_commands(user_commands)
    except TelegramAPIError:
        logging.warning("Не удалось обновить список команд")
    if ADMIN_ID:
        admin_commands = user_commands + [
            BotCommand(command="admin", description="Админка"),
            BotCommand(command="today", description="Записи на сегодня"),
            BotCommand(command="tomorrow", description="Записи на завтра"),
            BotCommand(command="week", description="Записи на неделю"),
            BotCommand(command="stats", description="Статистика"),
            BotCommand(command="find", description="Поиск по клиенту"),
            BotCommand(command="export", description="Выгрузка в CSV"),
            BotCommand(command="broadcast", description="Рассылка"),
        ]
        try:
            await bot.set_my_commands(admin_commands,
                                      scope=BotCommandScopeChat(chat_id=ADMIN_ID))
        except TelegramAPIError:
            logging.warning("Не удалось обновить команды админа (он не писал боту?)")


def make_error_handler(bot: Bot):
    async def on_error(event) -> bool:
        logging.error("Ошибка при обработке апдейта", exc_info=event.exception)
        if ADMIN_ID:
            summary = f"{type(event.exception).__name__}: {event.exception}"
            await send_plain(bot, ADMIN_ID, "⚠️ Ошибка в боте:\n" + summary[:3500])
        return True

    return on_error


async def main() -> None:
    setup_logging()
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬТЕ_ТОКЕН_ОТ_BOTFATHER":
        logging.error("Не задан BOT_TOKEN. Укажите его в переменной окружения или в .env.")
        raise SystemExit(1)
    if not ADMIN_ID:
        logging.warning("ADMIN_ID не задан: заявки не будут приходить владельцу. "
                        "Узнайте свой ID командой /id и добавьте его в .env.")

    init_db()
    bot = make_bot()
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    dispatcher.errors.register(make_error_handler(bot))
    dispatcher.startup.register(set_commands)

    await bot.delete_webhook(drop_pending_updates=True)
    reminder_task = asyncio.create_task(reminder_loop(bot))
    try:
        await dispatcher.start_polling(bot)
    finally:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
