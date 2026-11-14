import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton, KeyboardButton,
                           Message, ReplyKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------- НАСТРОЙКИ (меняйте под себя) ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_ОТ_BOTFATHER")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

SHOP_NAME = "СитиАкс"
ADDRESS = "ул. Академика Сахарова, 107/1"
PHONE = "+7 932 127-44-92"
WORK_TEXT = "Ежедневно 11:00-22:00, без выходных"
TELEGRAM_URL = "https://t.me/SHVHSA"

# (название, цена, срок). Цены ниже примерные, поставьте свои.
SERVICES = [
    ("Замена экрана", "от 3000 ₽", "около 1 часа"),
    ("Замена батареи", "от 1500 ₽", "30-40 минут"),
    ("Разъём зарядки", "от 1200 ₽", "около 1 часа"),
    ("Чистка после воды", "от 1500 ₽", "1-2 дня"),
    ("Диагностика", "бесплатно", "15-20 минут"),
]
WORK_HOURS = range(11, 22)   # слоты с 11:00 до 21:00
DAYS_AHEAD = 7
DAYS_OFF = set()             # выходных нет (например, {6} = воскресенье)
DB_PATH = "bookings.db"
# ---------------------------------------------------

logging.basicConfig(level=logging.INFO)
router = Router()
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


class Form(StatesGroup):
    model = State()
    name = State()
    phone = State()


# ---------- БАЗА ----------
def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, name TEXT, phone TEXT,
            service TEXT, model TEXT,
            day TEXT, time TEXT,
            reminded INTEGER DEFAULT 0,
            UNIQUE(day, time))"""
        )


def busy_slots(day):
    with db() as c:
        return {r[0] for r in c.execute("SELECT time FROM bookings WHERE day=?", (day,))}


def fmt_day(day):
    d = datetime.fromisoformat(day)
    return f"{d:%d.%m} ({WEEKDAYS[d.weekday()]})"


# ---------- КЛАВИАТУРЫ ----------
def menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Записаться")],
            [KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="💰 Цены")],
            [KeyboardButton(text="📍 Контакты")],
        ],
        resize_keyboard=True)


def services_kb():
    b = InlineKeyboardBuilder()
    for i, (name, price, _) in enumerate(SERVICES):
        b.button(text=f"{name} · {price}", callback_data=f"svc:{i}")
    b.adjust(1)
    return b.as_markup()


def days_kb():
    b = InlineKeyboardBuilder()
    today = datetime.now().date()
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        if d.weekday() in DAYS_OFF:
            continue
        label = "Сегодня" if i == 0 else f"{d:%d.%m} {WEEKDAYS[d.weekday()]}"
        b.button(text=label, callback_data=f"day:{d.isoformat()}")
    b.adjust(3)
    b.row(InlineKeyboardButton(text="⬅️ Изменить услугу", callback_data="back:svc"))
    return b.as_markup()


def times_kb(day):
    busy = busy_slots(day)
    now = datetime.now()
    b = InlineKeyboardBuilder()
    count = 0
    for h in WORK_HOURS:
        t = f"{h:02d}:00"
        if t in busy:
            continue
        if day == now.date().isoformat() and h <= now.hour:
            continue
        b.button(text=t, callback_data=f"time:{t}")
        count += 1
    b.adjust(3)
    b.row(InlineKeyboardButton(text="⬅️ Другой день", callback_data="back:day"))
    return b.as_markup(), count


def confirm_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Подтвердить запись", callback_data="confirm")
    b.button(text="❌ Отмена", callback_data="abort")
    b.adjust(1)
    return b.as_markup()


# ---------- ГЛАВНОЕ МЕНЮ ----------
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(f"Здравствуйте! Это {SHOP_NAME}, ремонт телефонов.\n"
                   "Выберите действие в меню внизу.", reply_markup=menu_kb())


@router.message(F.text == "📝 Записаться")
async def book(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Шаг 1 из 4. Что нужно отремонтировать?", reply_markup=services_kb())


@router.message(F.text == "💰 Цены")
async def prices(m: Message):
    lines = [f"• {n}: {p}, {t}" for n, p, t in SERVICES]
    await m.answer("Ориентировочные цены:\n\n" + "\n".join(lines) +
                   "\n\nТочную стоимость назовём после диагностики.")


@router.message(F.text == "📍 Контакты")
async def contacts(m: Message):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="💬 Написать в Telegram", url=TELEGRAM_URL))
    await m.answer(f"{SHOP_NAME}\nАдрес: {ADDRESS}\nТелефон: {PHONE}\nГрафик: {WORK_TEXT}",
                   reply_markup=kb.as_markup())


@router.message(F.text == "📋 Мои записи")
@router.message(Command("my"))
async def my(m: Message):
    today = datetime.now().date().isoformat()
    with db() as c:
        rows = c.execute(
            "SELECT id,day,time,service,model FROM bookings WHERE user_id=? AND day>=? ORDER BY day,time",
            (m.from_user.id, today)).fetchall()
    if not rows:
        return await m.answer("У вас нет активных записей. Нажмите «📝 Записаться».")
    b = InlineKeyboardBuilder()
    text = ["Ваши записи:\n"]
    for id_, day, t, svc, model in rows:
        text.append(f"{fmt_day(day)} в {t}: {svc}, {model}")
        b.button(text=f"Отменить {day[8:]}.{day[5:7]} {t}", callback_data=f"cancel:{id_}")
    b.adjust(1)
    await m.answer("\n".join(text), reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_booking(c: CallbackQuery, bot: Bot):
    id_ = int(c.data.split(":")[1])
    with db() as conn:
        row = conn.execute("SELECT user_id,day,time FROM bookings WHERE id=?", (id_,)).fetchone()
        if not row or row[0] != c.from_user.id:
            await c.answer("Запись уже отменена", show_alert=True)
            return
        conn.execute("DELETE FROM bookings WHERE id=?", (id_,))
    await c.message.edit_text(f"Запись на {fmt_day(row[1])} в {row[2]} отменена.")
    await c.answer()
    if ADMIN_ID:
        await bot.send_message(ADMIN_ID, f"Клиент отменил запись: {fmt_day(row[1])} {row[2]}")


# ---------- ЗАПИСЬ: ШАГИ ----------
@router.callback_query(F.data.startswith("svc:"))
async def pick_service(c: CallbackQuery, state: FSMContext):
    name, price, term = SERVICES[int(c.data.split(":")[1])]
    await state.update_data(service=name)
    await state.set_state(Form.model)
    await c.message.edit_text(
        f"Шаг 2 из 4.\nУслуга: {name}\nЦена: {price}, срок: {term}\n\n"
        "Напишите модель телефона (например, iPhone 12 или Redmi Note 11):")
    await c.answer()


@router.callback_query(F.data == "back:svc")
async def back_svc(c: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await c.message.edit_text("Шаг 1 из 4. Что нужно отремонтировать?", reply_markup=services_kb())
    await c.answer()


@router.message(Form.model, F.text)
async def get_model(m: Message, state: FSMContext):
    await state.update_data(model=m.text.strip())
    await m.answer("Шаг 3 из 4. Выберите день:", reply_markup=days_kb())


@router.callback_query(F.data == "back:day")
async def back_day(c: CallbackQuery):
    await c.message.edit_text("Шаг 3 из 4. Выберите день:", reply_markup=days_kb())
    await c.answer()


@router.callback_query(F.data.startswith("day:"))
async def pick_day(c: CallbackQuery, state: FSMContext):
    day = c.data.split(":", 1)[1]
    kb, count = times_kb(day)
    if count == 0:
        await c.answer("На этот день свободного времени нет", show_alert=True)
        return
    await state.update_data(day=day)
    await c.message.edit_text(f"Шаг 3 из 4. {fmt_day(day)}, выберите время:", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("time:"))
async def pick_time(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "day" not in data or "service" not in data:
        await c.message.answer("Давайте начнём заново: нажмите «📝 Записаться».")
        return await c.answer()
    t = c.data.split(":", 1)[1]
    if t in busy_slots(data["day"]):
        await c.answer("Это время только что заняли", show_alert=True)
        await c.message.edit_reply_markup(reply_markup=times_kb(data["day"])[0])
        return
    await state.update_data(time=t)
    await state.set_state(Form.name)
    await c.message.edit_text(f"Выбрано: {fmt_day(data['day'])} в {t}")
    await c.message.answer("Шаг 4 из 4. Как вас зовут?")
    await c.answer()


@router.message(Form.name, F.text)
async def get_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(Form.phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True)
    await m.answer("Отправьте номер телефона кнопкой ниже или напишите его вручную:", reply_markup=kb)


@router.message(Form.phone)
async def get_phone(m: Message, state: FSMContext):
    phone = m.contact.phone_number if m.contact else (m.text or "").strip()
    if len(phone) < 6:
        return await m.answer("Не похоже на номер, попробуйте ещё раз:")
    await state.update_data(phone=phone)
    d = await state.get_data()
    await m.answer("Номер принят.", reply_markup=menu_kb())
    await m.answer(
        "Проверьте запись:\n\n"
        f"Услуга: {d['service']}\nТелефон: {d['model']}\n"
        f"Когда: {fmt_day(d['day'])} в {d['time']}\n"
        f"Имя: {d['name']}\nНомер: {phone}",
        reply_markup=confirm_kb())


@router.callback_query(F.data == "abort")
async def abort(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Запись отменена. Чтобы начать заново, нажмите «📝 Записаться».")
    await c.answer()


@router.callback_query(F.data == "confirm")
async def confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    if not all(k in d for k in ("service", "model", "day", "time", "name", "phone")):
        await c.message.edit_text("Данные потерялись, начните заново: «📝 Записаться».")
        return await c.answer()
    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO bookings (user_id,name,phone,service,model,day,time) VALUES (?,?,?,?,?,?,?)",
                (c.from_user.id, d["name"], d["phone"], d["service"], d["model"], d["day"], d["time"]))
            booking_id = cur.lastrowid
    except sqlite3.IntegrityError:
        await c.message.edit_text("Это время только что заняли. Нажмите «📝 Записаться» и выберите другое.")
        await state.clear()
        return await c.answer()
    await state.clear()
    await c.message.edit_text(
        f"Вы записаны ✅\n\n{fmt_day(d['day'])} в {d['time']}\n{d['service']}, {d['model']}\n\n"
        "Накануне пришлю напоминание. Отменить запись можно в разделе «📋 Мои записи».")
    await c.answer()
    if ADMIN_ID:
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Подтвердить", callback_data=f"adm_ok:{booking_id}")
        kb.button(text="❌ Отклонить", callback_data=f"adm_no:{booking_id}")
        await bot.send_message(
            ADMIN_ID,
            f"Новая запись\n{fmt_day(d['day'])} {d['time']}\n{d['service']}, {d['model']}\n"
            f"{d['name']}, {d['phone']}\n@{c.from_user.username or 'без username'}",
            reply_markup=kb.as_markup())


# ---------- ДЛЯ ВЛАДЕЛЬЦА ----------
@router.callback_query(F.data.startswith("adm_"))
async def admin_action(c: CallbackQuery, bot: Bot):
    if c.from_user.id != ADMIN_ID:
        return await c.answer()
    action, id_ = c.data.split(":")
    id_ = int(id_)
    with db() as conn:
        row = conn.execute("SELECT user_id,day,time FROM bookings WHERE id=?", (id_,)).fetchone()
        if not row:
            await c.answer("Запись уже отменена", show_alert=True)
            return await c.message.edit_text(c.message.text + "\n\nЗапись уже отменена")
        if action == "adm_no":
            conn.execute("DELETE FROM bookings WHERE id=?", (id_,))
    user_id, day, t = row
    if action == "adm_ok":
        await bot.send_message(user_id, f"Ваша запись на {fmt_day(day)} в {t} подтверждена. Ждём вас!")
        await c.message.edit_text(c.message.text + "\n\n✅ Подтверждено")
    else:
        await bot.send_message(user_id, f"К сожалению, запись на {fmt_day(day)} в {t} не получится. "
                                        "Пожалуйста, выберите другое время: «📝 Записаться».")
        await c.message.edit_text(c.message.text + "\n\n❌ Отклонено")
    await c.answer()


@router.message(Command("today"))
async def today(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    day = datetime.now().date().isoformat()
    with db() as c:
        rows = c.execute("SELECT time,name,phone,service,model FROM bookings WHERE day=? ORDER BY time",
                         (day,)).fetchall()
    if not rows:
        return await m.answer("На сегодня записей нет.")
    await m.answer("Записи на сегодня:\n\n" +
                   "\n".join(f"{t}: {n}, {p}\n   {s}, {md}" for t, n, p, s, md in rows))


# ---------- НАПОМИНАНИЯ ----------
async def reminder_loop(bot: Bot):
    while True:
        try:
            now = datetime.now()
            if now.hour >= 12:
                tomorrow = (now.date() + timedelta(days=1)).isoformat()
                with db() as c:
                    rows = c.execute("SELECT id,user_id,time,service FROM bookings WHERE day=? AND reminded=0",
                                     (tomorrow,)).fetchall()
                    for id_, uid, t, svc in rows:
                        try:
                            await bot.send_message(uid, f"Напоминаю: завтра в {t} ждём вас в {SHOP_NAME} ({svc}).")
                        except Exception:
                            logging.exception("reminder failed")
                        c.execute("UPDATE bookings SET reminded=1 WHERE id=?", (id_,))
        except Exception:
            logging.exception("reminder loop")
        await asyncio.sleep(600)


async def main():
    init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
