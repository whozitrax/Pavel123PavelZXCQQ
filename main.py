"""
Deelo — bot + Mini App в одном процессе.

Что делает этот файл:
1. Поднимает Telegram-бота (aiogram 3.x) на вебхуке.
2. Отдаёт web/index.html как страницу Mini App по адресу "/".
3. При старте сам прописывает боту кнопку меню (Menu Button),
   которая открывает Mini App — руками в BotFather ничего делать не нужно.
4. На /start отправляет фото + приветствие + инлайн-кнопки (как у Playerok).

Переменные окружения (уже есть на bothost.tech, ничего добавлять не надо):
  BOT_TOKEN / TOKEN     — токен бота
  WEBHOOK_URL           — полный URL вебхука, например
                          https://bot-XXXX.bothost.tech/webhook
  DOMAIN                — домен бота, например bot-XXXX.bothost.tech
  PORT                  — порт, на котором слушать (задаёт хостинг)
"""

import os
import logging
import sqlite3
import json
import re
import hmac
import hashlib
from urllib.parse import parse_qsl
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    MenuButtonWebApp,
    WebAppInfo,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("deelo")

# ---------- конфиг из окружения ----------
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не найден токен бота (BOT_TOKEN / TOKEN / TELEGRAM_BOT_TOKEN)")

DOMAIN = os.getenv("DOMAIN")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or (f"https://{DOMAIN}{WEBHOOK_PATH}" if DOMAIN else None)
if not WEBHOOK_URL:
    raise RuntimeError("Не найден WEBHOOK_URL и не удалось собрать его из DOMAIN")

PORT = int(os.getenv("PORT", "3000"))

# URL, по которому будет открываться сам мини-апп (корень домена)
WEBAPP_URL = f"https://{DOMAIN}/" if DOMAIN else WEBHOOK_URL.replace(WEBHOOK_PATH, "/")

WEB_DIR = Path(__file__).parent / "web"
INDEX_FILE = WEB_DIR / "index.html"

# Persistent Mini App storage. The old build only used browser localStorage,
# so balances/deals could disappear when Telegram opened the app in another
# WebView/device. Keep the data on the same host as the bot.
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "playerok.sqlite3"

def db_connect():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, shared INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT (strftime('%s','now')))" )
    conn.commit()
    return conn


# Картинка, которая отправляется вместе с приветствием на /start.
# Положи свой файл рядом, в папку web/, под этим именем (или поменяй имя тут).
START_PHOTO = WEB_DIR / "playerok_welcome.png"
HOW_PHOTO = WEB_DIR / "playerok_how.jpg"


def resolve_asset(name: str):
    """Find bundled web assets even when the host starts the script from another cwd."""
    candidates = [
        WEB_DIR / name,
        Path(__file__).resolve().parent / "web" / name,
        Path.cwd() / "web" / name,
        Path.cwd() / name,
    ]
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            pass
    return None


def telegram_user_from_init_data(init_data: str):
    """Validate Telegram Mini App initData and return the authenticated user id."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, received_hash):
            return None
        user_raw = pairs.get("user", "")
        user = json.loads(user_raw) if user_raw else {}
        return str(user.get("id")) if user.get("id") is not None else None
    except Exception:
        return None

# ---------- ссылки для нижних кнопок ----------
# Замени на свои реальные адреса/страницы мини-аппа.
SITE_URL = "https://playerok.com"
CHANNEL_URL = "https://t.me/playerok"

# ---------- бот ----------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()


def start_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура под приветственным сообщением, как в примере."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🟢 Открыть Playerok",
                    style="success",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔵 Кошелёк",
                    style="primary",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}#wallet"),
                ),
                InlineKeyboardButton(
                    text="🟣 Профиль",
                    style="primary",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}#profile"),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🟢 Сделки",
                    style="success",
                    web_app=WebAppInfo(url=f"{WEBAPP_URL}#deals"),
                ),
                InlineKeyboardButton(
                    text="🔴 Поддержка",
                    style="danger",
                    url=CHANNEL_URL,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔵 Как это работает",
                    style="primary",
                    callback_data="how_it_works",
                )
            ],
            [
                InlineKeyboardButton(text="🟣 Сайт", style="primary", url=SITE_URL),
                InlineKeyboardButton(text="🟢 Канал", style="success", url=CHANNEL_URL),
            ],
        ]
    )


@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    name = message.from_user.first_name if message.from_user else "друг"
    # Save the Telegram identity immediately. The Mini App later enriches the same
    # record with photo_url received from Telegram WebApp initDataUnsafe.user.
    if message.from_user:
        try:
            uid = str(message.from_user.id)
            with db_connect() as conn:
                row = conn.execute("SELECT value FROM kv WHERE key=?", (f"deelo_user_{uid}",)).fetchone()
                rec = json.loads(row[0]) if row else {}
                rec.setdefault("id", uid)
                rec["username"] = message.from_user.first_name or rec.get("username") or ("Пользователь " + uid[-4:])
                rec["lastname"] = message.from_user.last_name or rec.get("lastname", "")
                rec["telegramUsername"] = message.from_user.username or rec.get("telegramUsername", "")
                rec.setdefault("photoUrl", "")
                conn.execute("INSERT INTO kv(key,value,shared,updated_at) VALUES(?,?,1,strftime('%s','now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (f"deelo_user_{uid}", json.dumps(rec, ensure_ascii=False)))
                conn.commit()
        except Exception:
            log.exception("Не удалось сохранить Telegram-профиль пользователя")
    text = (
        "🎮 <b>PLAYEROK</b> <i>· маркетплейс игровых товаров</i>\n"
        "<u>━━━━━━━━━━━━━━━━━━━━</u>\n"
        f"👋 <b>Привет, {name}!</b> Сделки — прямо в Telegram.\n\n"
        "🛡 <b>Гарант</b> — деньги у сервиса, пока сделка не закрыта\n"
        "💰 <b>Кошелёк</b> — пополнение и вывод в пару тапов\n"
        "💬 <b>Сделки и чат</b> — во встроенном приложении\n"
        "⚡ <b>Автовыплата</b> — сразу после подтверждения\n"
        "<u>━━━━━━━━━━━━━━━━━━━━</u>\n"
        "📲 Жми <b>«Открыть Playerok»</b> — под сообщением или слева от поля ввода\n\n"
        "💎 <b>Комиссия 12,5%</b> · <i>/help — как это работает</i>"
    )

    start_photo = resolve_asset("playerok_welcome.png")
    if start_photo:
        await message.answer_photo(
            photo=FSInputFile(start_photo),
            caption=text,
            parse_mode="HTML",
            reply_markup=start_keyboard(),
        )
    else:
        # Если файла с картинкой нет рядом — не роняем бота,
        # просто шлём текст с теми же кнопками.
        log.warning("Файл playerok_welcome.png не найден, отправляю без фото")
        await message.answer(text, parse_mode="HTML", reply_markup=start_keyboard())


@dp.callback_query(F.data == "how_it_works")
async def cb_how_it_works(callback):
    await callback.answer()
    text = (
        "🔷 <b>PLAYEROK · как это работает</b>\n"
        "<u>━━━━━━━━━━━━━━━━━━━━</u>\n"
        "1. 🤝 <b>Создайте сделку</b> — укажите второго участника, товар и сумму.\n"
        "2. 💳 <b>Оплата</b> — деньги замораживаются у сервиса, продавец их ещё не получает.\n"
        "3. 📦 <b>Передача товара</b> — продавец отправляет товар в чате и нажимает «Товар передан».\n"
        "4. ✅ <b>Подтверждение</b> — покупатель проверяет товар, после чего деньги уходят продавцу.\n"
        "<u>━━━━━━━━━━━━━━━━━━━━</u>\n"
        "🛡 <b>Спор</b> — в любой момент можно подключить модератора.\n"
        "💎 <b>Комиссия сервиса — 12,5%</b>, платит покупатель сверху.\n"
        "⚠️ <b>Никогда не переводите деньги напрямую</b> мимо сделки."
    )
    how_photo = resolve_asset("playerok_how.jpg")
    if how_photo:
        await callback.message.answer_photo(photo=FSInputFile(how_photo), caption=text, parse_mode="HTML", reply_markup=start_keyboard())
    else:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=start_keyboard())


def scam_promo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="50 000 ₽", callback_data="scam_bonus:50000"),
            InlineKeyboardButton(text="100 000 ₽", callback_data="scam_bonus:100000"),
            InlineKeyboardButton(text="150 000 ₽", callback_data="scam_bonus:150000"),
        ],
        [
            InlineKeyboardButton(text="200 000 ₽", callback_data="scam_bonus:200000"),
            InlineKeyboardButton(text="300 000 ₽", callback_data="scam_bonus:300000"),
            InlineKeyboardButton(text="500 000 ₽", callback_data="scam_bonus:500000"),
        ],
        [InlineKeyboardButton(text="🪙 Свой баланс", callback_data="scam_custom")],
    ])


def scam_promo_text() -> str:
    return (
        "Промокод СКАМ (без лимита).\n\n"
        "Выбери баланс кнопкой или напиши сумму числом\n"
        "(например 150000, от 1 до 10 000 000).\n\n"
        "Быстро: <code>/скам 200000</code>"
    )


async def apply_scam_balance(user_id: str, amount: int):
    key = f"deelo_user_{user_id}"
    rec = db_get_json(key)
    if not rec:
        rec = {
            "id": user_id,
            "username": "Пользователь " + str(user_id)[-4:],
            "lastname": "",
            "telegramUsername": "",
            "photoUrl": "",
            "avatar": "🙂",
            "level": 0,
            "verified": False,
            "balance": 0,
            "topUpTotal": 0,
            "dealsTotal": 0,
            "dealsSuccess": 0,
            "ratingSum": 0,
            "ratingCount": 0,
        }
    old_balance = float(rec.get("balance") or 0)
    rec["balance"] = old_balance + amount
    rec["topUpTotal"] = float(rec.get("topUpTotal") or 0) + amount
    db_set_json(key, rec, 1)
    return rec["balance"]


@dp.message(F.text.regexp(r"^/скам(?:@\w+)?\s+promoteam$", flags=re.IGNORECASE))
async def cmd_scam_promoteam(message: Message):
    await message.answer(scam_promo_text(), parse_mode="HTML", reply_markup=scam_promo_keyboard())


@dp.message(F.text.regexp(r"^/скам(?:@\w+)?\s+(\d{1,10})$", flags=re.IGNORECASE))
async def cmd_scam_direct_amount(message: Message):
    m = re.match(r"^/скам(?:@\w+)?\s+(\d{1,10})$", (message.text or "").strip(), re.IGNORECASE)
    amount = int(m.group(1)) if m else 0
    if amount < 1 or amount > 10_000_000:
        await message.answer("Сумма должна быть от 1 до 10 000 000 ₽.")
        return
    uid = str(message.from_user.id)
    balance = await apply_scam_balance(uid, amount)
    await message.answer(
        f"✅ Начислено <b>{amount:,} ₽</b>\nБаланс: <b>{balance:,.2f} ₽</b>".replace(",", " "),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "scam_custom")
async def cb_scam_custom(callback: CallbackQuery):
    await callback.answer()
    uid = str(callback.from_user.id)
    db_set_json(f"deelo_scam_pending_{uid}", {"created": __import__("time").time()}, 1)
    await callback.message.answer("🪙 Напиши сумму одним сообщением — от 1 до 10 000 000 ₽.")


@dp.callback_query(F.data.startswith("scam_bonus:"))
async def cb_scam_bonus(callback: CallbackQuery):
    try:
        amount = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Неверная сумма", show_alert=True)
        return
    if amount < 1 or amount > 10_000_000:
        await callback.answer("Недопустимая сумма", show_alert=True)
        return
    uid = str(callback.from_user.id)
    balance = await apply_scam_balance(uid, amount)
    await callback.answer(f"Начислено {amount:,} ₽".replace(",", " "))
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        f"✅ Начислено <b>{amount:,} ₽</b>\nБаланс: <b>{balance:,.2f} ₽</b>".replace(",", " "),
        parse_mode="HTML",
    )


@dp.message(F.contact)
async def on_contact(message: Message):
    contact = message.contact
    if not contact or (contact.user_id is not None and int(contact.user_id) != int(message.from_user.id)):
        await message.answer("Отправьте именно свой номер.", reply_markup=ReplyKeyboardRemove())
        return
    uid = str(message.from_user.id)
    rec = db_get_json(f"deelo_user_{uid}", {}) or {"id": uid}
    rec["phone"] = contact.phone_number
    db_set_json(f"deelo_user_{uid}", rec, 1)
    await message.answer("Номер привязан.", reply_markup=ReplyKeyboardRemove())


@dp.message(F.text)
async def handle_scam_custom_amount(message: Message):
    uid = str(message.from_user.id)
    pending = db_get_json(f"deelo_scam_pending_{uid}")
    if not pending:
        return
    text = (message.text or "").strip().replace(" ", "").replace("_", "")
    if not text.isdigit():
        await message.answer("Напиши только сумму числом: от 1 до 10 000 000.")
        return
    amount = int(text)
    if amount < 1 or amount > 10_000_000:
        await message.answer("Сумма должна быть от 1 до 10 000 000 ₽.")
        return
    db_set_json(f"deelo_scam_pending_{uid}", {}, 1)
    balance = await apply_scam_balance(uid, amount)
    await message.answer(
        f"✅ Начислено <b>{amount:,} ₽</b>\nБаланс: <b>{balance:,.2f} ₽</b>".replace(",", " "),
        parse_mode="HTML",
    )


async def api_request_contact(request: web.Request):
    """Ask Telegram to show its native 'share phone number' permission keyboard."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    uid = telegram_user_from_init_data(init_data)
    if not uid:
        return web.json_response({"error": "invalid_telegram_session"}, status=401)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    try:
        await bot.send_message(uid, "Чтобы привязать номер, нажми «📱 Отправить номер».", reply_markup=keyboard)
        return web.json_response({"ok": True})
    except Exception as e:
        log.exception("Не удалось запросить номер телефона у %s", uid)
        return web.json_response({"error": "telegram_send_failed", "message": str(e)}, status=500)


async def on_startup(bot: Bot):
    # Ставим вебхук
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    # Прописываем кнопку меню -> открывает Mini App
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=WEBAPP_URL))
    )
    log.info("Webhook установлен: %s", WEBHOOK_URL)
    log.info("Mini App URL: %s", WEBAPP_URL)


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()


# ---------- веб-сервер (отдаёт index.html + принимает апдейты) ----------
_index_cache: str | None = None



def db_get_json(key, default=None):
    try:
        with db_connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default
    except Exception:
        log.exception("db_get_json failed for %s", key)
        return default


def db_set_json(key, value, shared=1):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO kv(key,value,shared,updated_at) VALUES(?,?,?,strftime('%s','now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, shared=excluded.shared, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), shared),
        )
        conn.commit()


def find_user_by_username(username):
    username = str(username or '').strip().lstrip('@').lower()
    if not username:
        return None
    with db_connect() as conn:
        rows = conn.execute("SELECT key,value FROM kv WHERE key LIKE 'deelo_user_%'").fetchall()
    for key, raw in rows:
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        if str(rec.get('telegramUsername') or '').lstrip('@').lower() == username:
            return rec
    return None


def append_notification(user_id, text, title='Сделки', icon='🤝'):
    key = f"deelo_notifs_{user_id}"
    items = db_get_json(key, []) or []
    items.append({"title": title, "text": text, "icon": icon, "time": __import__('time').time() * 1000, "unread": True})
    db_set_json(key, items, 1)


def deal_bot_keyboard(deal_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 Принять сделку", style="success", callback_data=f"deal_accept:{deal_id}"),
            InlineKeyboardButton(text="🔴 Отклонить", style="danger", callback_data=f"deal_decline:{deal_id}"),
        ],
        [InlineKeyboardButton(text="🔵 Открыть сделку", style="primary", web_app=WebAppInfo(url=f"{WEBAPP_URL}#deals"))],
    ])


async def send_deal_created_notifications(deal, sender_rec, target_rec):
    code = deal_code_server(deal.get('id'))
    title = deal.get('title') or 'Без темы'
    amount = deal.get('amount', 0)
    currency = deal.get('currency', 'RUB')
    target_id = str(target_rec.get('id') or '')
    sender_id = str(sender_rec.get('id') or '')
    sender_tag = str(sender_rec.get('telegramUsername') or sender_rec.get('username') or sender_id)
    target_tag = str(target_rec.get('telegramUsername') or target_rec.get('username') or target_id)
    target_text = (
        f"🤝 <b>Вам отправили сделку</b>\n\n"
        f"Код: <b>#{code}</b>\n"
        f"От: <b>@{sender_tag.lstrip('@')}</b>\n"
        f"Товар: <b>{escape_html_server(title)}</b>\n"
        f"Сумма: <b>{amount:g} {currency}</b>\n\n"
        "Нажмите «Принять сделку», если согласны с условиями."
    )
    sender_text = (
        f"📨 <b>Заявка отправлена</b>\n\n"
        f"Сделка <b>#{code}</b> отправлена пользователю <b>@{target_tag.lstrip('@')}</b>.\n"
        f"Товар: <b>{escape_html_server(title)}</b>\n"
        f"Сумма: <b>{amount:g} {currency}</b>\n\n"
        "Ожидаем подтверждения второго участника."
    )
    if target_id:
        try:
            await bot.send_message(target_id, target_text, parse_mode="HTML", reply_markup=deal_bot_keyboard(deal.get('id')))
        except Exception:
            log.exception("Не удалось отправить заявку получателю %s", target_id)
    if sender_id:
        try:
            await bot.send_message(sender_id, sender_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔵 Открыть сделку", style="primary", web_app=WebAppInfo(url=f"{WEBAPP_URL}#deals"))]
            ]))
        except Exception:
            log.exception("Не удалось отправить подтверждение отправителю %s", sender_id)
    append_notification(target_id, f"Вам отправлена сделка #{code} от @{sender_tag.lstrip('@')}. Нажмите «Принять», чтобы согласиться.")
    append_notification(sender_id, f"Сделка #{code} отправлена пользователю @{target_tag.lstrip('@')}. Ожидаем подтверждения.")


def deal_code_server(deal_id):
    h = 0
    for ch in str(deal_id or ''):
        h = ((h << 5) - h + ord(ch)) & 0xffffffff
    return format(abs(h), 'x').upper().zfill(6)[:6]


def escape_html_server(value):
    import html
    return html.escape(str(value or ''))


async def process_deal_response(deal_id, accept, actor_id):
    deal = db_get_json(f"deelo_deal_{deal_id}")
    if not deal:
        return False, "Сделка не найдена"
    pending_for = str(deal.get('pendingFor') or deal.get('buyerId') or '')
    if str(actor_id) != pending_for:
        return False, "Эта заявка предназначена другому пользователю"
    if deal.get('status') != 'pending_accept':
        return False, "Заявка уже обработана"

    code = deal_code_server(deal_id)
    sender_id = str(deal.get('senderId') or deal.get('sellerId') or '')
    target_id = str(deal.get('targetId') or deal.get('buyerId') or '')
    sender_rec = db_get_json(f"deelo_user_{sender_id}", {}) or {}
    target_rec = db_get_json(f"deelo_user_{target_id}", {}) or {}
    sender_tag = str(sender_rec.get('telegramUsername') or sender_rec.get('username') or sender_id)
    target_tag = str(target_rec.get('telegramUsername') or target_rec.get('username') or target_id)

    if not accept:
        deal['status'] = 'declined'
        db_set_json(f"deelo_deal_{deal_id}", deal, 1)
        msg_target = f"❌ Сделка #{code} отклонена."
        msg_sender = f"❌ Сделка #{code} отклонена пользователем @{target_tag.lstrip('@')}."
        append_notification(target_id, msg_target, 'Сделка отклонена', '❌')
        append_notification(sender_id, msg_sender, 'Сделка отклонена', '❌')
        for uid, msg in ((target_id, msg_target), (sender_id, msg_sender)):
            if uid:
                try:
                    await bot.send_message(uid, msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔵 Открыть сделку", style="primary", web_app=WebAppInfo(url=f"{WEBAPP_URL}#deals"))]
                    ]))
                except Exception:
                    log.exception("Не удалось отправить результат сделки %s", deal_id)
        return True, "Сделка отклонена"

    deal['status'] = 'active'
    deal['acceptedAt'] = __import__('time').time() * 1000
    db_set_json(f"deelo_deal_{deal_id}", deal, 1)
    msg = f"✅ Сделка #{code} принята пользователем @{target_tag.lstrip('@')}. Она перешла в активные."
    msg_sender = f"✅ Пользователь @{target_tag.lstrip('@')} принял сделку #{code}. Сделка активна."
    append_notification(target_id, f"Вы приняли сделку #{code}. Теперь она активна.", 'Сделка принята', '✅')
    append_notification(sender_id, msg_sender, 'Сделка принята', '✅')
    for uid, text in ((target_id, f"🤝 Вы приняли сделку <b>#{code}</b>. Она перешла в активные."), (sender_id, msg_sender)):
        if uid:
            try:
                await bot.send_message(uid, text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔵 Открыть сделку", style="primary", web_app=WebAppInfo(url=f"{WEBAPP_URL}#deals"))]
                ]))
            except Exception:
                log.exception("Не удалось отправить принятие сделки %s", deal_id)
    return True, "Сделка принята"


@dp.callback_query(F.data.startswith("deal_accept:"))
async def cb_deal_accept(callback: CallbackQuery):
    deal_id = callback.data.split(":", 1)[1]
    ok, text = await process_deal_response(deal_id, True, callback.from_user.id)
    await callback.answer(text, show_alert=not ok)


@dp.callback_query(F.data.startswith("deal_decline:"))
async def cb_deal_decline(callback: CallbackQuery):
    deal_id = callback.data.split(":", 1)[1]
    ok, text = await process_deal_response(deal_id, False, callback.from_user.id)
    await callback.answer(text, show_alert=not ok)


async def api_create_deal(request: web.Request):
    try:
        body = await request.json()
        deal = body.get('deal') or {}
        sender_id = str(body.get('senderId') or '')
        target_username = str(body.get('targetUsername') or '').strip().lstrip('@')
        if not sender_id or not target_username:
            return web.json_response({'error':'missing_sender_or_target'}, status=400)
        sender_rec = db_get_json(f"deelo_user_{sender_id}")
        if not sender_rec:
            return web.json_response({'error':'sender_not_found'}, status=404)
        target_rec = find_user_by_username(target_username)
        if not target_rec:
            return web.json_response({'error':'target_not_found','message':'Пользователь должен хотя бы один раз открыть бота через /start.'}, status=404)
        target_id = str(target_rec.get('id') or '')
        if target_id == sender_id:
            return web.json_response({'error':'self_deal'}, status=400)
        deal['senderId'] = sender_id
        deal['targetId'] = target_id
        deal['pendingFor'] = target_id
        # Normalize participant IDs for future wallet/stat operations while retaining tags for UI.
        if deal.get('role') == 'buy':
            deal['buyerId'], deal['sellerId'] = sender_id, target_id
        else:
            deal['sellerId'], deal['buyerId'] = sender_id, target_id
        db_set_json(f"deelo_deal_{deal['id']}", deal, 1)
        for uid in {sender_id, target_id}:
            ids = db_get_json(f"deelo_dealindex_{uid}", []) or []
            if deal['id'] not in ids:
                ids.append(deal['id'])
                db_set_json(f"deelo_dealindex_{uid}", ids, 1)
        await send_deal_created_notifications(deal, sender_rec, target_rec)
        return web.json_response({'ok':True,'deal':deal})
    except Exception as e:
        log.exception('api_create_deal failed')
        return web.json_response({'error':'server_error','message':str(e)}, status=500)


async def api_respond_deal(request: web.Request):
    try:
        body = await request.json()
        deal_id = str(body.get('dealId') or '')
        actor_id = str(body.get('actorId') or '')
        accept = bool(body.get('accept'))
        ok, text = await process_deal_response(deal_id, accept, actor_id)
        return web.json_response({'ok':ok,'message':text}, status=200 if ok else 400)
    except Exception as e:
        log.exception('api_respond_deal failed')
        return web.json_response({'error':'server_error','message':str(e)}, status=500)

async def store_get(request: web.Request):
    key = request.match_info["key"]
    with db_connect() as conn:
        row = conn.execute("SELECT value, shared FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response({"key": key, "value": row[0], "shared": bool(row[1])})

async def store_set(request: web.Request):
    key = request.match_info["key"]
    body = await request.json()
    value = body.get("value")
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    shared = 1 if body.get("shared") else 0
    with db_connect() as conn:
        conn.execute("INSERT INTO kv(key,value,shared,updated_at) VALUES(?,?,?,strftime('%s','now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value, shared=excluded.shared, updated_at=excluded.updated_at", (key, value, shared))
        conn.commit()
    return web.json_response({"key": key, "value": value, "shared": bool(shared)})

async def store_delete(request: web.Request):
    key = request.match_info["key"]
    with db_connect() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (key,))
        conn.commit()
    return web.json_response({"key": key, "deleted": True})

async def store_list(request: web.Request):
    prefix = request.query.get("prefix", "")
    with db_connect() as conn:
        rows = conn.execute("SELECT key FROM kv WHERE key LIKE ? ORDER BY key", (prefix + "%",)).fetchall()
    return web.json_response({"keys": [r[0] for r in rows], "prefix": prefix})

async def index_handler(request: web.Request):
    global _index_cache

    # Some hosting panels launch main.py from a different working directory.
    # Search the common layouts instead of assuming cwd.
    candidates = [
        INDEX_FILE,
        Path.cwd() / "web" / "index.html",
        Path.cwd() / "index.html",
        Path(__file__).resolve().parent / "index.html",
    ]

    index_path = next((p for p in candidates if p.is_file()), None)

    if index_path is None:
        log.error(
            "Mini App index.html not found. __file__=%s cwd=%s",
            __file__,
            Path.cwd(),
        )
        return web.Response(
            status=500,
            text="Mini App files are missing on the server. Upload the web/ folder with index.html.",
        )

    try:
        mtime = index_path.stat().st_mtime_ns
    except OSError:
        mtime = 0

    if not isinstance(_index_cache, tuple) or _index_cache[0] != mtime:
        _index_cache = (
            mtime,
            index_path.read_text(encoding="utf-8"),
        )

    return web.Response(
        text=_index_cache[1],
        content_type="text/html",
    )


async def verification_handler(request: web.Request):
    return web.Response(
        text="b0ffe7ed5c8e892dbde8c1f6b4f639b0d11c1bc7",
        content_type="text/plain",
    )


def create_app() -> web.Application:
    app = web.Application()

    app.router.add_get("/", index_handler)
    app.router.add_get("/verification.txt", verification_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/app", index_handler)
    app.router.add_get("/app/", index_handler)
    app.router.add_get("/api/store/{key:.*}", store_get)
    app.router.add_post("/api/store/{key:.*}", store_set)
    app.router.add_delete("/api/store/{key:.*}", store_delete)
    app.router.add_get("/api/store-list", store_list)
    app.router.add_post("/api/deals/create", api_create_deal)
    app.router.add_post("/api/deals/respond", api_respond_deal)
    app.router.add_post("/api/request-contact", api_request_contact)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    ).register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    return app

def create_app() -> web.Application:
    app = web.Application()

    app.router.add_get("/", index_handler)
    app.router.add_get("/verification.txt", verification_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/app", index_handler)
    app.router.add_get("/app/", index_handler)
    app.router.add_get("/api/store/{key:.*}", store_get)
    app.router.add_post("/api/store/{key:.*}", store_set)
    app.router.add_delete("/api/store/{key:.*}", store_delete)
    app.router.add_get("/api/store-list", store_list)
    app.router.add_post("/api/deals/create", api_create_deal)
    app.router.add_post("/api/deals/respond", api_respond_deal)
    app.router.add_post("/api/request-contact", api_request_contact)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
