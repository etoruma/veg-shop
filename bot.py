"""
Бот магазина овощей + приём заказов из Telegram Mini App.

Переменные окружения:
  BOT_TOKEN    — токен от @BotFather
  ADMIN_ID     — ваш Telegram ID (узнать: @userinfobot), сюда приходят заказы
  WEBAPP_URL   — HTTPS-адрес Mini App (страница index.html)
  PAY_DETAILS  — (необязательно) реквизиты, их увидит клиент при оплате переводом
  DB_PATH      — путь к базе (по умолчанию data/shop.db)
  CATALOG_PATH — путь к catalog.json (по умолчанию рядом с ботом)

Цены берутся из catalog.json на сервере: что бы ни прислал клиент,
сумма пересчитывается здесь.
"""
import asyncio
import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, KeyboardButton, Message,
                           ReplyKeyboardMarkup, WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
PAY_DETAILS = os.getenv("PAY_DETAILS", "")
DB_PATH = os.getenv("DB_PATH", "data/shop.db")
CATALOG_PATH = os.getenv("CATALOG_PATH", "catalog.json")

with open(CATALOG_PATH, encoding="utf-8") as f:
    CATALOG = json.load(f)
SHOP = CATALOG["shop"]
PRODUCTS = {p["id"]: p for p in CATALOG["products"]}
STEP = SHOP.get("step", 0.5)
CUR = SHOP["currency"]

STATUS = {"new": "🆕 Новый", "accepted": "✅ Принят", "delivering": "🚚 В пути",
          "done": "🎉 Выполнен", "cancelled": "❌ Отменён"}
USER_MSG = {
    "accepted": "✅ Заказ №{id} принят, уже собираем!",
    "delivering": "🚚 Заказ №{id} в пути — скоро будем у вас.",
    "done": "🎉 Заказ №{id} выполнен. Спасибо, что выбрали нас!",
    "cancelled": "❌ Заказ №{id} отменён. Если это ошибка — напишите нам.",
}
PAY = {"cash": "💵 Наличными при получении", "transfer": "📲 Переводом"}

router = Router()


# ---------- Утилиты ----------
def money(n: float) -> str:
    return f"{round(n):,}".replace(",", " ") + f" {CUR}"


def kg(q: float) -> str:
    return f"{q:g}".replace(".", ",") + f" {SHOP.get('unit', 'кг')}"


def shop_kb() -> ReplyKeyboardMarkup:
    # Важно: sendData из Mini App работает только при запуске с кнопки клавиатуры
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🥬 Открыть магазин",
                                  web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True, is_persistent=True)


# ---------- База ----------
def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db():
    with closing(db()) as con, con:
        con.execute("CREATE TABLE IF NOT EXISTS orders("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, "
                    "status TEXT, data TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)")


def save_order(user_id: int, o: dict) -> int:
    with closing(db()) as con, con:
        cur = con.execute("INSERT INTO orders(user_id, status, data) VALUES(?,?,?)",
                          (user_id, "new", json.dumps(o, ensure_ascii=False)))
        return cur.lastrowid


def get_order(oid: int):
    with closing(db()) as con:
        row = con.execute("SELECT user_id, status, data FROM orders WHERE id=?",
                          (oid,)).fetchone()
    return (row[0], row[1], json.loads(row[2])) if row else None


def set_status(oid: int, status: str):
    with closing(db()) as con, con:
        con.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))


# ---------- Проверка заказа (цены считаем сами) ----------
def build_order(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("неверные данные")
    raw = data.get("items")
    if not isinstance(raw, list) or not 0 < len(raw) <= 30:
        raise ValueError("корзина пуста")

    qtys: dict[str, float] = {}
    for it in raw:
        pid = it.get("id") if isinstance(it, dict) else None
        qty = it.get("qty") if isinstance(it, dict) else None
        if pid not in PRODUCTS or isinstance(qty, bool) or not isinstance(qty, (int, float)):
            raise ValueError("неизвестный товар")
        if not 0 < qty <= 30 or abs(qty / STEP - round(qty / STEP)) > 1e-9:
            raise ValueError("неверное количество")
        qtys[pid] = qtys.get(pid, 0) + qty
    if any(q > 30 for q in qtys.values()):
        raise ValueError("слишком большое количество")

    lines = []
    for pid, qty in qtys.items():
        p = PRODUCTS[pid]
        lines.append({"name": p["name"], "emoji": p["emoji"], "qty": qty,
                      "price": p["price"], "sum": p["price"] * qty})
    sub = sum(l["sum"] for l in lines)
    if sub < SHOP["min_order"]:
        raise ValueError(f"минимальный заказ {money(SHOP['min_order'])}")
    delivery = 0 if sub >= SHOP["free_from"] else SHOP["delivery_fee"]

    name = str(data.get("name", "")).strip()[:60]
    phone = str(data.get("phone", "")).strip()[:25]
    address = str(data.get("address", "")).strip()[:200]
    comment = str(data.get("comment", "")).strip()[:300]
    pay = data.get("pay")
    if len(name) < 2 or len(address) < 5:
        raise ValueError("заполните имя и адрес")
    if not 10 <= len(re.sub(r"\D", "", phone)) <= 15:
        raise ValueError("проверьте номер телефона")
    if pay not in PAY:
        raise ValueError("не выбран способ оплаты")

    return {"lines": lines, "sub": sub, "delivery": delivery, "total": sub + delivery,
            "name": name, "phone": phone, "address": address,
            "comment": comment, "pay": pay}


def order_text(o: dict, oid: int, status: str, user=None) -> str:
    items = "\n".join(f"{l['emoji']} {escape(l['name'])} — {kg(l['qty'])} × "
                      f"{money(l['price'])} = <b>{money(l['sum'])}</b>"
                      for l in o["lines"])
    text = (f"🧾 <b>Заказ №{oid}</b> · {STATUS[status]}\n\n{items}\n\n"
            f"Товары: {money(o['sub'])}\n"
            f"Доставка: {money(o['delivery']) if o['delivery'] else 'бесплатно'}\n"
            f"<b>Итого: {money(o['total'])}</b>\n\n"
            f"👤 {escape(o['name'])}\n📞 {escape(o['phone'])}\n"
            f"📍 {escape(o['address'])}\n💳 {PAY[o['pay']]}")
    if o["comment"]:
        text += f"\n💬 {escape(o['comment'])}"
    if user:
        text += f"\n\n<a href=\"tg://user?id={user.id}\">Написать клиенту</a>"
    return text


def admin_kb(oid: int):
    b = InlineKeyboardBuilder()
    b.button(text="✅ Принять", callback_data=f"st:{oid}:accepted")
    b.button(text="🚚 В путь", callback_data=f"st:{oid}:delivering")
    b.button(text="🎉 Выполнен", callback_data=f"st:{oid}:done")
    b.button(text="❌ Отмена", callback_data=f"st:{oid}:cancelled")
    b.adjust(2, 2)
    return b.as_markup()


# ---------- Хендлеры ----------
@router.message(CommandStart())
async def start(m: Message):
    if not WEBAPP_URL:
        return await m.answer("⚠️ Не задан WEBAPP_URL — витрина пока недоступна.")
    await m.answer(f"👋 Привет, {escape(m.from_user.first_name)}!\n\n"
                   f"<b>{escape(SHOP['name'])}</b> — свежие овощи с доставкой.\n"
                   "Нажмите кнопку ниже, чтобы открыть витрину 👇",
                   reply_markup=shop_kb())


@router.message(F.web_app_data)
async def on_order(m: Message, bot: Bot):
    try:
        o = build_order(json.loads(m.web_app_data.data))
    except (ValueError, json.JSONDecodeError) as e:
        return await m.answer(f"⚠️ Не удалось оформить заказ: {e}.\nПопробуйте ещё раз.",
                              reply_markup=shop_kb())
    oid = save_order(m.from_user.id, o)

    extra = ""
    if o["pay"] == "transfer":
        extra = ("\n\n💳 " + escape(PAY_DETAILS)) if PAY_DETAILS else \
                "\n\n💳 Реквизиты для перевода пришлём в ближайшее время."
    await m.answer("✅ <b>Заказ принят!</b> Мы скоро свяжемся.\n\n"
                   + order_text(o, oid, "new") + extra, reply_markup=shop_kb())

    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, order_text(o, oid, "new", m.from_user),
                                   reply_markup=admin_kb(oid))
        except Exception:
            logging.exception("Не удалось отправить заказ админу")
    else:
        logging.warning("ADMIN_ID не задан — заказ №%s сохранён только в базе", oid)


@router.callback_query(F.data.startswith("st:"))
async def change_status(c: CallbackQuery, bot: Bot):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Нет доступа", show_alert=True)
    _, oid, st = c.data.split(":")
    found = get_order(int(oid))
    if not found or st not in USER_MSG:
        return await c.answer("Заказ не найден", show_alert=True)
    user_id, _, o = found
    set_status(int(oid), st)
    kb = None if st in ("done", "cancelled") else admin_kb(int(oid))
    await c.message.edit_text(
        order_text(o, int(oid), st) + f"\n\n<a href=\"tg://user?id={user_id}\">Написать клиенту</a>",
        reply_markup=kb)
    try:
        await bot.send_message(user_id, USER_MSG[st].format(id=oid))
    except Exception:
        logging.exception("Клиенту не доставлено уведомление")
    await c.answer("Готово")


@router.message(Command("orders"), F.from_user.id == ADMIN_ID)
async def last_orders(m: Message):
    with closing(db()) as con:
        rows = con.execute("SELECT id, status, data FROM orders "
                           "ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        return await m.answer("Заказов пока нет.")
    lines = []
    for oid, st, data in rows:
        o = json.loads(data)
        lines.append(f"№{oid} · {STATUS[st]} · {money(o['total'])} · {escape(o['name'])}")
    await m.answer("<b>Последние заказы</b>\n\n" + "\n".join(lines))


# ---------- Запуск ----------
async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
