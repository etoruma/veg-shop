"""
Бот магазина брокколи + приём заказов из Telegram Mini App.

Обязательные переменные окружения:
  BOT_TOKEN         — токен от @BotFather
  ADMIN_ID          — ваш Telegram ID (узнать: @userinfobot)
  WEBAPP_URL        — HTTPS-адрес витрины (страница index.html)
Необязательные:
  SUPPORT_USERNAME  — username поддержки без @ (иначе клиенты пишут вам)
  PAY_DETAILS       — реквизиты для оплаты переводом
  REMIND_DAYS       — через сколько дней напомнить о повторном заказе (по умолчанию 7)
  API_PORT          — порт мини-сервера остатков (для живых остатков в витрине)
  DB_PATH           — путь к базе (по умолчанию data/shop.db)
  CATALOG_PATH      — путь к catalog.json

Цены, скидки, слоты и остатки проверяются здесь, на сервере:
что бы ни прислал клиент, сумма пересчитывается заново.
"""
import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import string
from contextlib import closing
from datetime import datetime, timedelta
from html import escape
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, ErrorEvent, FSInputFile,
                           KeyboardButton, Message, ReplyKeyboardMarkup,
                           WebAppInfo)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
SUPPORT = os.getenv("SUPPORT_USERNAME", "").lstrip("@")
PAY_DETAILS = os.getenv("PAY_DETAILS", "")
REMIND_DAYS = int(os.getenv("REMIND_DAYS", "7") or 7)
API_PORT = int(os.getenv("API_PORT", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "data/shop.db")
CATALOG_PATH = os.getenv("CATALOG_PATH", "catalog.json")

with open(CATALOG_PATH, encoding="utf-8") as f:
    CATALOG = json.load(f)
SHOP = CATALOG["shop"]
PRODUCTS = {p["id"]: p for p in CATALOG["products"]}
PAYMENTS = {p["id"]: p for p in SHOP["payments"]}
DELIVERY = SHOP["delivery"]
CUR = SHOP["currency"]
TZ = ZoneInfo(SHOP.get("tz", "UTC"))

STATUS = {"new": "🆕 Новый", "accepted": "✅ Принят", "delivering": "🚚 В пути",
          "done": "🎉 Выполнен", "cancelled": "❌ Отменён"}
USER_MSG = {
    "accepted": "✅ Заказ №{id} принят — уже собираем!",
    "delivering": "🚚 Заказ №{id} в пути, скоро будем у вас.",
    "done": "🎉 Заказ №{id} доставлен. Приятного аппетита!",
    "cancelled": "❌ Заказ №{id} отменён. Если это ошибка — напишите нам.",
}
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
WDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

router = Router()
pending_broadcast: dict[int, str] = {}


# ======================= Утилиты =======================
def r(x: float) -> int:
    """Округление «половина вверх» — совпадает с витриной."""
    return int(x + 0.5 + 1e-9)


def money(n: float) -> str:
    return f"{r(n):,}".replace(",", " ") + f" {CUR}"


def qs(q: float) -> str:
    return f"{q:g}".replace(".", ",")


def fmt_qty(pid: str, q: float) -> str:
    return f"{qs(q)} {PRODUCTS[pid]['unit']}"


def now_local() -> datetime:
    return datetime.now(TZ)


def date_label(iso: str, today) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    diff = (d - today).days
    base = f"{d.day} {MONTHS[d.month - 1]}"
    if diff == 0:
        return f"Сегодня, {base}"
    if diff == 1:
        return f"Завтра, {base}"
    return f"{WDAYS[d.weekday()]}, {base}"


def base_items(pid: str, qty: float) -> dict[str, float]:
    """Раскрывает наборы в базовые товары (для остатков и сборки)."""
    p = PRODUCTS[pid]
    if p.get("contains"):
        out: dict[str, float] = {}
        for c in p["contains"]:
            out[c["id"]] = out.get(c["id"], 0) + c["qty"] * qty
        return out
    return {pid: qty}


def slot_options(now: datetime) -> list[tuple[str, list[str]]]:
    """Доступные (дата, [окна]) — та же логика, что в витрине."""
    out = []
    for i in range(DELIVERY["days_ahead"] + 1):
        day = now + timedelta(days=i)
        if day.weekday() in DELIVERY.get("days_off", []):
            continue
        slots = []
        for s in DELIVERY["slots"]:
            if i == 0:
                if now.hour >= DELIVERY["cutoff_hour"]:
                    continue
                if int(s.split(":")[0]) < now.hour + DELIVERY["lead_hours"]:
                    continue
            slots.append(s)
        if slots:
            out.append((day.date().isoformat(), slots))
    return out


def tg_link(user_id: int, label: str = "Написать клиенту") -> str:
    return f'<a href="tg://user?id={user_id}">{label}</a>'


async def send_long(m: Message, text: str):
    """Telegram режет сообщения длиннее 4096 символов — режем сами по абзацам."""
    chunk = ""
    for part in text.split("\n\n"):
        if len(chunk) + len(part) + 2 > 3800:
            await m.answer(chunk)
            chunk = ""
        chunk += ("\n\n" if chunk else "") + part
    if chunk:
        await m.answer(chunk)


def shop_kb() -> ReplyKeyboardMarkup:
    # sendData из Mini App работает только при запуске с кнопки клавиатуры
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🥦 Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))],
            [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="💬 Поддержка")],
        ],
        resize_keyboard=True, is_persistent=True)


# ======================= База данных =======================
def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def ensure_column(con, table: str, col: str, ddl: str):
    cols = [row["name"] for row in con.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def init_db():
    with closing(db()) as con, con:
        con.execute("CREATE TABLE IF NOT EXISTS users("
                    "user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, "
                    "joined TEXT DEFAULT CURRENT_TIMESTAMP, subscribed INTEGER DEFAULT 1)")
        con.execute("CREATE TABLE IF NOT EXISTS orders("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT, "
                    "data TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)")
        for col, ddl in (("username", "TEXT"), ("done_at", "TEXT"),
                         ("reminded", "INTEGER DEFAULT 0"), ("nudged", "INTEGER DEFAULT 0"),
                         ("rating", "INTEGER")):
            ensure_column(con, "orders", col, ddl)
        ensure_column(con, "users", "ref_by", "INTEGER")
        con.execute("CREATE TABLE IF NOT EXISTS promos("
                    "code TEXT PRIMARY KEY, percent INTEGER, active INTEGER DEFAULT 1, "
                    "max_uses INTEGER DEFAULT 0, uses INTEGER DEFAULT 0)")
        con.execute("CREATE TABLE IF NOT EXISTS promo_uses("
                    "code TEXT, user_id INTEGER, order_id INTEGER, PRIMARY KEY(code, user_id))")
        con.execute("CREATE TABLE IF NOT EXISTS stock(pid TEXT PRIMARY KEY, qty REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        w = SHOP.get("welcome_promo")
        if w:
            con.execute("INSERT OR IGNORE INTO promos(code, percent) VALUES(?,?)",
                        (w["code"].upper(), w["percent"]))


def touch_user(u, ref: int | None = None):
    with closing(db()) as con, con:
        is_new = con.execute("SELECT 1 FROM users WHERE user_id=?", (u.id,)).fetchone() is None
        con.execute("INSERT INTO users(user_id, username, first_name) VALUES(?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
                    "first_name=excluded.first_name", (u.id, u.username, u.first_name))
        if is_new and ref and ref != u.id and \
                con.execute("SELECT 1 FROM users WHERE user_id=?", (ref,)).fetchone():
            con.execute("UPDATE users SET ref_by=? WHERE user_id=?", (ref, u.id))


def has_orders(uid: int) -> bool:
    with closing(db()) as con:
        return con.execute("SELECT 1 FROM orders WHERE user_id=? AND status!='cancelled' LIMIT 1",
                           (uid,)).fetchone() is not None


def meta_get(k: str, default: str = "") -> str:
    with closing(db()) as con:
        row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(k: str, v: str):
    with closing(db()) as con, con:
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?,?)", (k, v))


def pause_state() -> tuple[bool, str]:
    return meta_get("paused") == "1", meta_get("pause_note")


def referral_reward(oid: int):
    """После первой доставки приглашённого выдаёт пригласившему личный промокод.
    Возвращает (id_пригласившего, код, процент) или None."""
    cfg = SHOP.get("referral")
    if not cfg:
        return None
    with closing(db()) as con, con:
        row = con.execute("SELECT o.user_id, u.ref_by FROM orders o JOIN users u "
                          "ON u.user_id=o.user_id WHERE o.id=?", (oid,)).fetchone()
        if not row or not row["ref_by"]:
            return None
        done = con.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='done'",
                           (row["user_id"],)).fetchone()[0]
        if done != 1:
            return None
        code = "REF" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        con.execute("INSERT INTO promos(code, percent, max_uses) VALUES(?,?,1)",
                    (code, cfg["reward_percent"]))
        return row["ref_by"], code, cfg["reward_percent"]


# ======================= Проверка и создание заказа =======================
def validate_order(data) -> dict:
    """Проверяет данные из витрины и считает суммы по ценам из catalog.json."""
    paused, note = pause_state()
    if paused:
        raise ValueError("приём заказов временно приостановлен" + (f": {note}" if note else ""))
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
        p = PRODUCTS[pid]
        if p.get("available") is False:
            raise ValueError(f"{p['name']} временно недоступен")
        step = p.get("step", 1)
        if qty < p.get("min", step) - 1e-9 or abs(qty / step - round(qty / step)) > 1e-9:
            raise ValueError(f"неверное количество: {p['name']}")
        qtys[pid] = qtys.get(pid, 0) + qty
    for pid, q in qtys.items():
        if q > PRODUCTS[pid].get("max", 30) + 1e-9:
            raise ValueError(f"слишком большое количество: {PRODUCTS[pid]['name']}")

    lines = []
    for pid, qty in qtys.items():
        p = PRODUCTS[pid]
        lines.append({"id": pid, "name": p["name"], "emoji": p["emoji"], "qty": qty,
                      "unit": p["unit"], "price": p["price"], "sum": r(p["price"] * qty)})
    sub = sum(l["sum"] for l in lines)
    if sub < SHOP["min_order"]:
        raise ValueError(f"минимальный заказ {money(SHOP['min_order'])}")
    delivery = 0 if sub >= SHOP["free_from"] else SHOP["delivery_fee"]

    date, slot = data.get("date"), data.get("slot")
    if not any(d == date and slot in slots for d, slots in slot_options(now_local())):
        raise ValueError("выбранное время доставки уже недоступно — выберите другое окно")

    name = str(data.get("name", "")).strip()[:60]
    phone = str(data.get("phone", "")).strip()[:25]
    address = str(data.get("address", "")).strip()[:200]
    comment = str(data.get("comment", "")).strip()[:300]
    pay = data.get("pay")
    promo = re.sub(r"[^A-Z0-9_-]", "", str(data.get("promo", "")).upper())[:20]
    if len(name) < 2 or len(address) < 5:
        raise ValueError("заполните имя и адрес")
    if not 10 <= len(re.sub(r"\D", "", phone)) <= 15:
        raise ValueError("проверьте номер телефона")
    if pay not in PAYMENTS:
        raise ValueError("не выбран способ оплаты")

    return {"lines": lines, "sub": sub, "delivery": delivery, "discount": 0,
            "total": sub + delivery, "name": name, "phone": phone, "address": address,
            "comment": comment, "pay": pay, "date": date, "slot": slot,
            "promo_code": promo, "promo": None, "consumed": {}}


def place_order(user, o: dict) -> tuple[int, list[str]]:
    """Резервирует остатки, применяет промокод, сохраняет заказ. Всё в одной транзакции."""
    notes: list[str] = []
    with closing(db()) as con, con:
        recent = con.execute("SELECT COUNT(*) FROM orders WHERE user_id=? "
                             "AND created > datetime('now','-1 hour')", (user.id,)).fetchone()[0]
        if recent >= 5:
            raise ValueError("слишком много заказов за час, попробуйте позже или напишите нам")

        need: dict[str, float] = {}
        for l in o["lines"]:
            for bid, q in base_items(l["id"], l["qty"]).items():
                need[bid] = need.get(bid, 0) + q
        tracked = {}
        for bid, q in need.items():
            row = con.execute("SELECT qty FROM stock WHERE pid=?", (bid,)).fetchone()
            if row is not None:
                tracked[bid] = q
                if row["qty"] + 1e-9 < q:
                    name = PRODUCTS[bid]["name"]
                    if row["qty"] <= 1e-9:
                        raise ValueError(f"{name} закончился")
                    raise ValueError(f"{name}: осталось только {fmt_qty(bid, row['qty'])}")

        code = o["promo_code"]
        if code:
            p = con.execute("SELECT * FROM promos WHERE code=?", (code,)).fetchone()
            if not p or not p["active"]:
                notes.append(f"Промокод {code} не найден или не действует.")
            elif p["max_uses"] and p["uses"] >= p["max_uses"]:
                notes.append(f"Промокод {code} уже исчерпан.")
            elif con.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?",
                             (code, user.id)).fetchone():
                notes.append(f"Промокод {code} уже использован вами.")
            else:
                o["promo"] = {"code": code, "percent": p["percent"]}
                o["discount"] = r(o["sub"] * p["percent"] / 100)
        o["total"] = o["sub"] - o["discount"] + o["delivery"]

        for bid, q in tracked.items():
            con.execute("UPDATE stock SET qty = qty - ? WHERE pid=?", (q, bid))
        o["consumed"] = tracked

        cur = con.execute("INSERT INTO orders(user_id, username, status, data) VALUES(?,?,?,?)",
                          (user.id, user.username, "new", json.dumps(o, ensure_ascii=False)))
        oid = cur.lastrowid
        if o["promo"]:
            con.execute("INSERT INTO promo_uses VALUES(?,?,?)", (o["promo"]["code"], user.id, oid))
            con.execute("UPDATE promos SET uses = uses + 1 WHERE code=?", (o["promo"]["code"],))
    return oid, notes


def release(con, oid: int, o: dict):
    """Возвращает остатки и промокод при отмене заказа."""
    for bid, q in (o.get("consumed") or {}).items():
        con.execute("UPDATE stock SET qty = qty + ? WHERE pid=?", (q, bid))
    if o.get("promo"):
        if con.execute("DELETE FROM promo_uses WHERE order_id=?", (oid,)).rowcount:
            con.execute("UPDATE promos SET uses = MAX(uses - 1, 0) WHERE code=?",
                        (o["promo"]["code"],))


def update_status(oid: int, st: str):
    """Возвращает (user_id, order, старый_статус, изменился_ли) или None."""
    with closing(db()) as con, con:
        row = con.execute("SELECT user_id, status, data FROM orders WHERE id=?", (oid,)).fetchone()
        if not row:
            return None
        o, old = json.loads(row["data"]), row["status"]
        if old == "cancelled" or old == st:
            return row["user_id"], o, old, False
        if st == "cancelled":
            release(con, oid, o)
        con.execute("UPDATE orders SET status=?, done_at=CASE WHEN ?='done' "
                    "THEN datetime('now') ELSE done_at END WHERE id=?", (st, st, oid))
        return row["user_id"], o, old, True


def get_order(oid: int):
    with closing(db()) as con:
        row = con.execute("SELECT user_id, status, data FROM orders WHERE id=?", (oid,)).fetchone()
    return (row["user_id"], row["status"], json.loads(row["data"])) if row else None


# ======================= Тексты и клавиатуры =======================
def order_text(o: dict, oid: int, status: str, user_id: int | None = None) -> str:
    items = "\n".join(f"{l['emoji']} {escape(l['name'])} — {qs(l['qty'])} {l['unit']} × "
                      f"{money(l['price'])} = <b>{money(l['sum'])}</b>" for l in o["lines"])
    pay = PAYMENTS.get(o.get("pay"), {})
    text = f"🧾 <b>Заказ №{oid}</b> · {STATUS[status]}\n\n{items}\n\nТовары: {money(o['sub'])}\n"
    if o.get("discount"):
        text += f"🎟 {o['promo']['code']} −{o['promo']['percent']}%: −{money(o['discount'])}\n"
    text += (f"Доставка: {money(o['delivery']) if o['delivery'] else 'бесплатно'}\n"
             f"<b>Итого: {money(o['total'])}</b>\n\n")
    if o.get("date"):
        text += f"📅 {date_label(o['date'], now_local().date())} · {escape(o['slot'])}\n"
    text += (f"👤 {escape(o['name'])}\n📞 {escape(o['phone'])}\n📍 {escape(o['address'])}\n"
             f"💳 {escape(pay.get('title', '?'))} {escape(pay.get('hint', ''))}"
             + (" · 💰 оплачено" if o.get("paid") else ""))
    if o.get("comment"):
        text += f"\n💬 {escape(o['comment'])}"
    if user_id:
        text += f"\n\n{tg_link(user_id)}"
    return text


def admin_kb(oid: int, paid: bool = False, final: bool = False):
    b = InlineKeyboardBuilder()
    if not final:
        b.button(text="✅ Принять", callback_data=f"st:{oid}:accepted")
        b.button(text="🚚 В путь", callback_data=f"st:{oid}:delivering")
        b.button(text="🎉 Выполнен", callback_data=f"st:{oid}:done")
        b.button(text="❌ Отмена", callback_data=f"st:{oid}:cancelled")
    b.button(text="💰 Оплата получена ✓" if paid else "💰 Отметить оплату",
             callback_data=f"pd:{oid}")
    b.adjust(*((2, 2, 1) if not final else (1,)))
    return b.as_markup()


def admin_menu_kb():
    paused, _ = pause_state()
    b = InlineKeyboardBuilder()
    b.button(text="📋 Сборка сегодня", callback_data="ad:pick0")
    b.button(text="📋 Сборка завтра", callback_data="ad:pick1")
    b.button(text="🧾 Активные заказы", callback_data="ad:orders")
    b.button(text="📊 Итоги за 7 дней", callback_data="ad:stats")
    b.button(text="📦 Остатки", callback_data="ad:stock")
    b.button(text="🎟 Промокоды", callback_data="ad:promo")
    b.button(text="▶️ Возобновить приём заказов" if paused else "⏸ Пауза приёма заказов",
             callback_data="ad:pause")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def cancel_kb(oid: int):
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отменить заказ", callback_data=f"cancel:{oid}")
    return b.as_markup()


def rate_kb(oid: int):
    b = InlineKeyboardBuilder()
    for n in range(1, 6):
        b.button(text=f"{n}⭐", callback_data=f"rate:{oid}:{n}")
    b.adjust(5)
    return b.as_markup()


def open_shop_kb():
    b = InlineKeyboardBuilder()
    b.button(text="🥦 Открыть магазин", callback_data="open")
    return b.as_markup()


# ======================= Команды клиента =======================
@router.message(CommandStart())
async def start(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    touch_user(m.from_user, int(arg[3:]) if arg.startswith("ref") and arg[3:].isdigit() else None)
    if not WEBAPP_URL:
        return await m.answer("⚠️ Не задан WEBAPP_URL — витрина пока недоступна.")
    perks = "\n".join(f"{t['icon']} <b>{escape(t['title'])}</b> — {escape(t['text'])}"
                      for t in SHOP.get("trust", []))
    text = (f"👋 Привет, {escape(m.from_user.first_name or 'друг')}!\n\n"
            f"<b>{escape(SHOP['name'])}</b> — {escape(SHOP['tagline'])}.\n\n{perks}\n\n")
    w = SHOP.get("welcome_promo")
    if w and not has_orders(m.from_user.id):
        text += f"🎁 Промокод на первый заказ: <code>{escape(w['code'])}</code> (−{w['percent']}%)\n\n"
    text += "Нажмите «🥦 Открыть магазин» внизу 👇"
    await m.answer(text, reply_markup=shop_kb())


@router.callback_query(F.data == "open")
async def open_shop(c: CallbackQuery):
    await c.message.answer("Откройте магазин кнопкой внизу 👇", reply_markup=shop_kb())
    await c.answer()


@router.message(F.text == "💬 Поддержка")
async def support(m: Message):
    if SUPPORT:
        await m.answer(f"Напишите нам: @{escape(SUPPORT)}\nОтвечаем в течение дня.")
    elif ADMIN_ID:
        await m.answer(f"Напишите нам: {tg_link(ADMIN_ID, 'связаться с магазином')}")
    else:
        await m.answer("Контакты поддержки пока не настроены.")


@router.message(F.text == "📦 Мои заказы")
async def my_orders(m: Message):
    with closing(db()) as con:
        rows = con.execute("SELECT id, status, data FROM orders WHERE user_id=? "
                           "ORDER BY id DESC LIMIT 5", (m.from_user.id,)).fetchall()
    if not rows:
        return await m.answer("Заказов пока нет. Загляните в магазин 🥦", reply_markup=shop_kb())
    for row in reversed(rows):
        await m.answer(order_text(json.loads(row["data"]), row["id"], row["status"]),
                       reply_markup=cancel_kb(row["id"]) if row["status"] == "new" else None)


@router.message(Command("unsubscribe"))
async def unsubscribe(m: Message):
    with closing(db()) as con, con:
        con.execute("UPDATE users SET subscribed=0 WHERE user_id=?", (m.from_user.id,))
    await m.answer("Хорошо, рассылки и напоминания отключены. Вернуть: /subscribe")


@router.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer("🥦 <b>Как заказать</b>\n1. Нажмите «🥦 Открыть магазин» внизу\n"
                   "2. Выберите сорт и время доставки\n3. Мы подтвердим заказ в чате\n\n"
                   "/invite — пригласить друга и получить скидку\n"
                   "/unsubscribe — отключить рассылки", reply_markup=shop_kb())


@router.message(Command("invite"))
async def invite(m: Message, bot: Bot):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref{m.from_user.id}"
    w, cfg = SHOP.get("welcome_promo"), SHOP.get("referral")
    text = "🎁 <b>Пригласите друзей</b>\n\n"
    if w:
        text += f"Другу — промокод <code>{escape(w['code'])}</code> (−{w['percent']}%) на первый заказ.\n"
    if cfg:
        text += f"Вам — личный промокод −{cfg['reward_percent']}% после его первой доставки.\n"
    await m.answer(text + f"\nВаша ссылка:\n{link}")


@router.message(Command("subscribe"))
async def subscribe(m: Message):
    touch_user(m.from_user)
    with closing(db()) as con, con:
        con.execute("UPDATE users SET subscribed=1 WHERE user_id=?", (m.from_user.id,))
    await m.answer("Отлично, будем иногда напоминать о свежей брокколи 🥦")


# ======================= Приём заказа =======================
@router.message(F.web_app_data)
async def on_order(m: Message, bot: Bot):
    touch_user(m.from_user)
    try:
        o = validate_order(json.loads(m.web_app_data.data))
        oid, notes = place_order(m.from_user, o)
    except ValueError as e:
        return await m.answer(f"⚠️ Не удалось оформить заказ: {escape(str(e))}.\n"
                              "Откройте магазин и попробуйте ещё раз.", reply_markup=shop_kb())

    text = "✅ <b>Заказ принят!</b> Подтвердим в ближайшее время.\n\n" + order_text(o, oid, "new")
    if notes:
        text += "\n\n⚠️ " + " ".join(escape(n) for n in notes)
    if o["pay"] == "transfer":
        text += ("\n\n💳 " + escape(PAY_DETAILS)) if PAY_DETAILS else \
                "\n\n💳 Реквизиты для перевода пришлём в ближайшее время."
    text += "\n\nПока заказ не принят, его можно отменить кнопкой ниже."
    await m.answer(text, reply_markup=cancel_kb(oid))

    if ADMIN_ID:
        admin_text = order_text(o, oid, "new", m.from_user.id)
        if notes:
            admin_text += "\n\n⚠️ " + " ".join(escape(n) for n in notes)
        try:
            await bot.send_message(ADMIN_ID, admin_text, reply_markup=admin_kb(oid))
        except Exception:
            logging.exception("Не удалось отправить заказ админу")
    else:
        logging.warning("ADMIN_ID не задан — заказ №%s только в базе", oid)


@router.callback_query(F.data.startswith("st:"))
async def change_status(c: CallbackQuery, bot: Bot):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Нет доступа", show_alert=True)
    _, oid, st = c.data.split(":")
    if st not in USER_MSG:
        return await c.answer("Неизвестный статус", show_alert=True)
    res = update_status(int(oid), st)
    if not res:
        return await c.answer("Заказ не найден", show_alert=True)
    user_id, o, old, changed = res
    if old == "cancelled":
        return await c.answer("Заказ уже отменён", show_alert=True)
    if not changed:
        return await c.answer("Без изменений")
    kb = None if st == "cancelled" else admin_kb(int(oid), o.get("paid", False), final=(st == "done"))
    try:
        await c.message.edit_text(order_text(o, int(oid), st, user_id), reply_markup=kb)
    except Exception:
        logging.exception("edit_text")
    try:
        await bot.send_message(user_id, USER_MSG[st].format(id=oid))
        if st == "done":
            await bot.send_message(user_id, "Как вам заказ? Оценка поможет нам стать лучше 👇",
                                   reply_markup=rate_kb(int(oid)))
    except Exception:
        logging.exception("Клиенту не доставлено уведомление")
    if st == "done":
        reward = referral_reward(int(oid))
        if reward:
            ref_id, code, pct = reward
            try:
                await bot.send_message(ref_id, f"🎁 Ваш друг получил первый заказ! Дарим промокод "
                                               f"<code>{code}</code> — −{pct}% на следующий заказ.")
            except Exception:
                logging.exception("Не удалось отправить награду за приглашение")
    await c.answer("Готово")


@router.callback_query(F.data.startswith("pd:"))
async def toggle_paid(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Нет доступа", show_alert=True)
    oid = int(c.data.split(":")[1])
    with closing(db()) as con, con:
        row = con.execute("SELECT user_id, status, data FROM orders WHERE id=?", (oid,)).fetchone()
        if row:
            o = json.loads(row["data"])
            o["paid"] = not o.get("paid")
            con.execute("UPDATE orders SET data=? WHERE id=?", (json.dumps(o, ensure_ascii=False), oid))
    if not row:
        return await c.answer("Заказ не найден", show_alert=True)
    st = row["status"]
    kb = None if st == "cancelled" else admin_kb(oid, o["paid"], final=(st == "done"))
    try:
        await c.message.edit_text(order_text(o, oid, st, row["user_id"]), reply_markup=kb)
    except Exception:
        logging.exception("edit_text")
    await c.answer("Оплата отмечена 💰" if o["paid"] else "Отметка снята")


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_order(c: CallbackQuery, bot: Bot):
    oid = int(c.data.split(":")[1])
    found = get_order(oid)
    if not found or found[0] != c.from_user.id:
        return await c.answer("Это не ваш заказ", show_alert=True)
    if found[1] != "new":
        return await c.answer("Заказ уже в работе. Чтобы изменить его, напишите в поддержку.",
                              show_alert=True)
    update_status(oid, "cancelled")
    await c.message.edit_text(order_text(found[2], oid, "cancelled"))
    await c.answer("Заказ отменён")
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"❌ Клиент отменил заказ №{oid} "
                                             f"({escape(found[2]['name'])}). {tg_link(c.from_user.id)}")
        except Exception:
            logging.exception("notify admin about cancel")


@router.callback_query(F.data.startswith("rate:"))
async def rate(c: CallbackQuery, bot: Bot):
    _, oid, n = c.data.split(":")
    found = get_order(int(oid))
    if not found or found[0] != c.from_user.id:
        return await c.answer("Это не ваш заказ", show_alert=True)
    with closing(db()) as con, con:
        con.execute("UPDATE orders SET rating=? WHERE id=?", (int(n), int(oid)))
    await c.message.edit_text(f"Спасибо за оценку! {'⭐' * int(n)}")
    await c.answer()
    if int(n) <= 3 and ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID, f"😕 Низкая оценка {n}⭐ по заказу №{oid} ({escape(found[2]['name'])}). "
                          f"Свяжитесь с клиентом: {tg_link(c.from_user.id)}")
        except Exception:
            logging.exception("notify admin about rating")


# ======================= Админ-команды =======================
ADMIN = F.from_user.id == ADMIN_ID


@router.message(Command("admin"), ADMIN)
async def admin_help(m: Message):
    await m.answer(
        "<b>Команды администратора</b>\n\n"
        "/orders — активные заказы\n"
        "/pick — лист сборки на сегодня; <code>/pick tomorrow</code> или <code>/pick 2026-10-15</code>\n"
        "/stats — итоги за 7 дней; <code>/stats 30</code> за 30\n"
        "/stock — остатки; <code>/stock calabrese 25</code> задать, "
        "<code>/stock calabrese +10</code> добавить, <code>/stock calabrese off</code> не учитывать\n"
        "/promo — промокоды; <code>/promo add SPRING15 15 [лимит]</code>, "
        "<code>/promo off CODE</code>, <code>/promo on CODE</code>\n"
        "/pause [причина] и /resume — пауза приёма заказов\n"
        "/broadcast текст — рассылка подписчикам", reply_markup=admin_menu_kb())


@router.callback_query(F.data.startswith("ad:"))
async def admin_menu_cb(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Нет доступа", show_alert=True)
    act, m, today = c.data.split(":")[1], c.message, now_local().date()
    await c.answer()
    if act in ("pick0", "pick1"):
        await send_long(m, pick_text((today + timedelta(days=int(act[-1]))).isoformat(), today))
    elif act == "orders":
        await active_orders(m)
    elif act == "stats":
        await m.answer(stats_text(7))
    elif act == "stock":
        await stock_cmd(m, SimpleNamespace(args=""))
    elif act == "promo":
        await promo_cmd(m, SimpleNamespace(args=""))
    elif act == "pause":
        paused, _ = pause_state()
        meta_set("paused", "0" if paused else "1")
        await m.answer("▶️ Приём заказов возобновлён." if paused else
                       "⏸ Приём заказов приостановлен: клиенты увидят это в витрине и при оформлении.",
                       reply_markup=admin_menu_kb())


@router.message(Command("pause"), ADMIN)
async def pause_cmd(m: Message, command: CommandObject):
    meta_set("paused", "1")
    meta_set("pause_note", (command.args or "").strip()[:200])
    await m.answer("⏸ Приём заказов приостановлен. Вернуть: /resume")


@router.message(Command("resume"), ADMIN)
async def resume_cmd(m: Message):
    meta_set("paused", "0")
    meta_set("pause_note", "")
    await m.answer("▶️ Приём заказов возобновлён.")


@router.message(Command("orders"), ADMIN)
async def active_orders(m: Message):
    with closing(db()) as con:
        rows = con.execute("SELECT id, status, data FROM orders WHERE status IN ('new','accepted','delivering') "
                           "ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        return await m.answer("Активных заказов нет 🎉")
    today = now_local().date()
    lines = []
    for row in rows:
        o = json.loads(row["data"])
        when = f"{date_label(o['date'], today)} {o['slot']}" if o.get("date") else "—"
        lines.append(f"№{row['id']} · {STATUS[row['status']]} · {when} · {money(o['total'])} · {escape(o['name'])}")
    await m.answer("<b>Активные заказы</b>\n\n" + "\n".join(lines))


def pick_text(day_iso: str, today) -> str:
    with closing(db()) as con:
        rows = con.execute("SELECT id, status, data FROM orders "
                           "WHERE status IN ('new','accepted','delivering')").fetchall()
    orders = [(row["id"], row["status"], json.loads(row["data"])) for row in rows]
    orders = [x for x in orders if x[2].get("date") == day_iso]
    title = date_label(day_iso, today)
    if not orders:
        return f"На {title} активных заказов нет."
    totals: dict[str, float] = {}
    for _, _, o in orders:
        for l in o["lines"]:
            for bid, q in base_items(l["id"], l["qty"]).items():
                totals[bid] = totals.get(bid, 0) + q
    pack = "\n".join(f"• {escape(PRODUCTS[b]['name'])}: <b>{fmt_qty(b, q)}</b>" for b, q in totals.items())
    text = f"📋 <b>Лист сборки — {title}</b>\nЗаказов: {len(orders)}\n\n🧺 <b>Собрать всего:</b>\n{pack}"
    for slot in DELIVERY["slots"]:
        slot_orders = [x for x in orders if x[2].get("slot") == slot]
        if not slot_orders:
            continue
        blocks = []
        for oid, st, o in slot_orders:
            items = ", ".join(f"{escape(l['name'])} {qs(l['qty'])} {l['unit']}" for l in o["lines"])
            pay = PAYMENTS.get(o["pay"], {}).get("title", "?")
            blocks.append(f"№{oid} {STATUS[st]}\n{escape(o['name'])} · {escape(o['phone'])}\n"
                          f"📍 {escape(o['address'])}\n{items}\n{escape(pay)} · <b>{money(o['total'])}</b>")
        text += f"\n\n🕐 <b>{escape(slot)}</b>\n\n" + "\n\n".join(blocks)
    return text


@router.message(Command("pick"), ADMIN)
async def pick(m: Message, command: CommandObject):
    today = now_local().date()
    arg = (command.args or "").strip().lower()
    if arg in ("", "today", "сегодня"):
        day = today
    elif arg in ("tomorrow", "завтра"):
        day = today + timedelta(days=1)
    else:
        try:
            day = datetime.strptime(arg, "%Y-%m-%d").date()
        except ValueError:
            return await m.answer("Формат: /pick, /pick tomorrow или /pick 2026-10-15")
    await send_long(m, pick_text(day.isoformat(), today))


def stats_text(days: int) -> str:
    with closing(db()) as con:
        rows = con.execute("SELECT user_id, status, data, rating FROM orders "
                           "WHERE created >= datetime('now', ?)", (f"-{days} days",)).fetchall()
        new_users = con.execute("SELECT COUNT(*) FROM users WHERE joined >= datetime('now', ?)",
                                (f"-{days} days",)).fetchone()[0]
        buyers = {row["user_id"] for row in rows if row["status"] != "cancelled"}
        repeat = 0
        for uid in buyers:
            n = con.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status!='cancelled'",
                            (uid,)).fetchone()[0]
            repeat += 1 if n > 1 else 0
    live = [(row, json.loads(row["data"])) for row in rows if row["status"] != "cancelled"]
    cancelled = len(rows) - len(live)
    revenue = sum(o["total"] for _, o in live)
    done = sum(1 for row, _ in live if row["status"] == "done")
    avg = revenue / len(live) if live else 0
    top: dict[str, int] = {}
    for _, o in live:
        for l in o["lines"]:
            top[l["name"]] = top.get(l["name"], 0) + l["sum"]
    top_text = "\n".join(f"• {escape(n)} — {money(s)}" for n, s in
                         sorted(top.items(), key=lambda x: -x[1])[:5]) or "—"
    ratings = [row["rating"] for row, _ in live if row["rating"]]
    rating_line = (f"⭐ Средняя оценка: {sum(ratings) / len(ratings):.1f} ({len(ratings)})"
                   if ratings else "⭐ Оценок пока нет")
    promo = sum(1 for _, o in live if o.get("promo"))
    unpaid = [o for row, o in live if row["status"] == "done" and not o.get("paid")]
    return (f"📊 <b>Итоги за {days} дн.</b>\n\n"
            f"🆕 Новых подписчиков: {new_users}\n"
            f"🧾 Заказов: {len(live)} (выполнено {done}, отменено {cancelled})\n"
            f"💰 Выручка: <b>{money(revenue)}</b>\n"
            f"🧮 Средний чек: {money(avg)}\n"
            f"🔁 Повторные покупатели: {repeat} из {len(buyers)}\n"
            f"🎟 Заказов с промокодом: {promo}\n"
            f"💸 Выполнено без отметки об оплате: {len(unpaid)} на {money(sum(o['total'] for o in unpaid))}\n"
            f"{rating_line}\n\n"
            f"🏆 <b>Топ по выручке</b>\n{top_text}")


@router.message(Command("stats"), ADMIN)
async def stats(m: Message, command: CommandObject):
    try:
        days = max(1, min(365, int((command.args or "7").strip())))
    except ValueError:
        days = 7
    await m.answer(stats_text(days))


@router.message(Command("stock"), ADMIN)
async def stock_cmd(m: Message, command: CommandObject):
    args = (command.args or "").split()
    if not args:
        with closing(db()) as con:
            rows = {row["pid"]: row["qty"] for row in con.execute("SELECT pid, qty FROM stock")}
        lines = []
        for pid, p in PRODUCTS.items():
            if p.get("contains"):
                continue
            lines.append(f"• <code>{pid}</code> {escape(p['name'])}: " +
                         (f"<b>{fmt_qty(pid, rows[pid])}</b>" if pid in rows else "не учитывается"))
        return await m.answer("<b>Остатки</b>\n\n" + "\n".join(lines) +
                              "\n\nЗадать: <code>/stock id 25</code>, добавить: <code>/stock id +10</code>, "
                              "выключить учёт: <code>/stock id off</code>")
    pid = args[0]
    if pid not in PRODUCTS or PRODUCTS[pid].get("contains") or len(args) != 2:
        return await m.answer("Формат: /stock id 25 | /stock id +10 | /stock id off\n"
                              "(id — как в catalog.json, наборы отдельно не учитываются)")
    val = args[1].lower()
    with closing(db()) as con, con:
        if val == "off":
            con.execute("DELETE FROM stock WHERE pid=?", (pid,))
            return await m.answer(f"Учёт «{PRODUCTS[pid]['name']}» выключен.")
        try:
            amount = float(val.replace(",", "."))
        except ValueError:
            return await m.answer("Количество должно быть числом.")
        row = con.execute("SELECT qty FROM stock WHERE pid=?", (pid,)).fetchone()
        new = ((row["qty"] if row else 0) + amount) if val.startswith("+") else amount
        new = max(0.0, new)
        con.execute("INSERT INTO stock(pid, qty) VALUES(?,?) ON CONFLICT(pid) DO UPDATE SET qty=excluded.qty",
                    (pid, new))
    await m.answer(f"{escape(PRODUCTS[pid]['name'])}: теперь <b>{fmt_qty(pid, new)}</b>")


@router.message(Command("promo"), ADMIN)
async def promo_cmd(m: Message, command: CommandObject):
    args = (command.args or "").split()
    with closing(db()) as con, con:
        if not args or args[0] == "list":
            rows = con.execute("SELECT * FROM promos ORDER BY code").fetchall()
            lines = [f"<code>{row['code']}</code> −{row['percent']}% · использован {row['uses']}"
                     + (f"/{row['max_uses']}" if row["max_uses"] else "")
                     + (" · ✅" if row["active"] else " · ⛔️") for row in rows]
            return await m.answer("<b>Промокоды</b>\n\n" + ("\n".join(lines) or "пусто") +
                                  "\n\n/promo add CODE 15 [лимит] · /promo off CODE · /promo on CODE")
        if args[0] == "add" and len(args) >= 3:
            code = args[1].upper()
            if not re.fullmatch(r"[A-Z0-9_-]{3,20}", code) or not args[2].isdigit() \
                    or not 1 <= int(args[2]) <= 90:
                return await m.answer("Код: 3–20 символов (латиница, цифры), скидка 1–90%.")
            limit = int(args[3]) if len(args) > 3 and args[3].isdigit() else 0
            con.execute("INSERT OR REPLACE INTO promos(code, percent, active, max_uses, uses) "
                        "VALUES(?,?,1,?,COALESCE((SELECT uses FROM promos WHERE code=?),0))",
                        (code, int(args[2]), limit, code))
            return await m.answer(f"Промокод <code>{code}</code> −{args[2]}% создан"
                                  + (f", лимит {limit}." if limit else ", без лимита."))
        if args[0] in ("on", "off") and len(args) == 2:
            con.execute("UPDATE promos SET active=? WHERE code=?",
                        (1 if args[0] == "on" else 0, args[1].upper()))
            return await m.answer("Готово.")
    await m.answer("Формат: /promo, /promo add CODE 15 [лимит], /promo off CODE, /promo on CODE")


@router.message(Command("broadcast"), ADMIN)
async def broadcast(m: Message, command: CommandObject):
    text = (command.args or "").strip()
    if not text:
        return await m.answer("Формат: /broadcast Текст рассылки (HTML-разметка допустима)")
    with closing(db()) as con:
        n = con.execute("SELECT COUNT(*) FROM users WHERE subscribed=1").fetchone()[0]
    pending_broadcast[m.from_user.id] = text
    b = InlineKeyboardBuilder()
    b.button(text=f"📣 Отправить {n} подписчикам", callback_data="bc:yes")
    b.button(text="Отмена", callback_data="bc:no")
    b.adjust(1)
    await m.answer("<b>Предпросмотр:</b>\n\n" + text, reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("bc:"))
async def broadcast_confirm(c: CallbackQuery, bot: Bot):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Нет доступа", show_alert=True)
    text = pending_broadcast.pop(c.from_user.id, None)
    if c.data == "bc:no" or not text:
        await c.message.edit_text("Рассылка отменена.")
        return await c.answer()
    await c.message.edit_text("Отправляю…")
    with closing(db()) as con:
        ids = [row["user_id"] for row in con.execute("SELECT user_id FROM users WHERE subscribed=1")]
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text + "\n\n<i>Отписаться: /unsubscribe</i>",
                                   reply_markup=open_shop_kb())
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.06)
    await c.message.edit_text(f"✅ Рассылка завершена: доставлено {ok} из {len(ids)}.")
    await c.answer()


@router.error()
async def on_error(event: ErrorEvent):
    logging.error("Ошибка в обработчике: %s", event.exception, exc_info=event.exception)


# ======================= Фоновые задачи =======================
async def nudge_admin(bot: Bot):
    if not ADMIN_ID:
        return
    with closing(db()) as con, con:
        rows = con.execute("SELECT id, data FROM orders WHERE status='new' AND nudged=0 "
                           "AND created < datetime('now','-20 minutes')").fetchall()
        for row in rows:
            con.execute("UPDATE orders SET nudged=1 WHERE id=?", (row["id"],))
    for row in rows:
        o = json.loads(row["data"])
        await bot.send_message(ADMIN_ID, f"⏰ Заказ №{row['id']} ({escape(o['name'])}, {money(o['total'])}) "
                                         "ждёт подтверждения уже 20+ минут. Команда /orders.")


async def send_reminders(bot: Bot):
    with closing(db()) as con, con:
        rows = con.execute(
            "SELECT o.id, o.user_id FROM orders o JOIN users u ON u.user_id=o.user_id "
            "WHERE o.status='done' AND o.reminded=0 AND u.subscribed=1 AND o.done_at < datetime('now', ?) "
            "AND NOT EXISTS (SELECT 1 FROM orders n WHERE n.user_id=o.user_id AND n.status!='cancelled' "
            "AND n.created > o.done_at)", (f"-{REMIND_DAYS} days",)).fetchall()
        for row in rows:
            con.execute("UPDATE orders SET reminded=1 WHERE id=?", (row["id"],))
    for row in rows:
        try:
            await bot.send_message(row["user_id"], "🥦 Пора за свежей брокколи? Закажите заново за минуту — "
                                                   "мы запомнили ваши данные.\n\n<i>Отписаться: /unsubscribe</i>",
                                   reply_markup=open_shop_kb())
        except Exception:
            pass
        await asyncio.sleep(0.06)


async def daily_backup(bot: Bot):
    if not ADMIN_ID:
        return
    now = now_local()
    if now.hour < 3:
        return
    today = now.date().isoformat()
    with closing(db()) as con:
        row = con.execute("SELECT v FROM meta WHERE k='last_backup'").fetchone()
    if row and row["v"] == today:
        return
    folder = os.path.dirname(DB_PATH) or "."
    path = os.path.join(folder, f"backup-{today}.db")
    with closing(db()) as src, closing(sqlite3.connect(path)) as dst:
        src.backup(dst)
    await bot.send_document(ADMIN_ID, FSInputFile(path), caption=f"💾 Резервная копия базы за {today}")
    with closing(db()) as con, con:
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('last_backup', ?)", (today,))
    for fn in os.listdir(folder):
        if fn.startswith("backup-") and fn != os.path.basename(path):
            try:
                os.remove(os.path.join(folder, fn))
            except OSError:
                pass


async def maintenance(bot: Bot):
    while True:
        for job in (nudge_admin, send_reminders, daily_backup):
            try:
                await job(bot)
            except Exception:
                logging.exception("Фоновая задача %s", job.__name__)
        await asyncio.sleep(60)


async def start_api():
    """Мини-сервер остатков: витрина показывает «Осталось N» и «Нет в наличии»."""
    if not API_PORT:
        return

    hdr = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}

    def read_stock():
        with closing(db()) as con:
            return {row["pid"]: row["qty"] for row in con.execute("SELECT pid, qty FROM stock")}

    async def stock_handler(request):
        return web.json_response(read_stock(), headers=hdr)

    async def state_handler(request):
        paused, note = pause_state()
        return web.json_response({"stock": read_stock(), "paused": paused, "note": note}, headers=hdr)

    app = web.Application()
    app.router.add_get("/api/stock", stock_handler)
    app.router.add_get("/api/state", state_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", API_PORT).start()
    logging.info("API остатков запущен на порту %s", API_PORT)


# ======================= Запуск =======================
async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")
    if not ADMIN_ID:
        logging.warning("ADMIN_ID не задан: уведомления о заказах приходить не будут")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await start_api()
    background = asyncio.create_task(maintenance(bot))  # noqa: F841 (держим ссылку)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
