import os
import re
import json
import time
import hmac
import sqlite3
import asyncio
import hashlib
import logging
import urllib.parse
from datetime import datetime
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, WebAppInfo, InlineKeyboardMarkup,
    InlineKeyboardButton, PreCheckoutQuery,
    LabeledPrice, CallbackQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import catalog  # catalog.py + prices.json (narxlar shu yerdan olinadi)


def load_env():
    env_path = Path(__file__).with_name(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]
# Har bir sotuvdan foyda (2 coin) shu hisobga tushadi. .env da PROFIT_USER_ID
# yozilmasa, birinchi admin hisobiga tushadi.
PROFIT_USER_ID = int(os.getenv("PROFIT_USER_ID", "0") or 0) or (
    ADMIN_IDS[0] if ADMIN_IDS else 0
)

DB_PATH = os.getenv("DB_PATH", "gamedonate.db")
PORT = int(os.getenv("PORT", "8080"))
COIN_VALUE = 1000  # 1 coin = 1000 so'm

# Stars orqali coin sotib olish: 1 coin uchun nechta Star.
STARS_PER_COIN = int(os.getenv("STARS_PER_COIN", "7"))

STATUS_PENDING = "Kutilmoqda"
STATUS_DONE = "Bajarildi"
STATUS_CANCEL = "Bekor qilindi"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GameDonate")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# catalog.GAMES dagi har bir o'yin/xizmat uchun logotiplar va ma'lumotlar
GAME_META = {
    "pubg": {
        "emoji": "🎯", 
        "logo": "https://cdn.iconscout.com/icon/free/png-256/free-pubg-logo-icon-download-in-svg-png-gif-file-formats--gamers-gamin-pack-logos-icons-3215355.png",
        "domain": "pubgmobile.com", 
        "id_label": "PUBG Player ID"
    },
    "mlbb": {
        "emoji": "⚔️", 
        "logo": "https://upload.wikimedia.org/wikipedia/en/c/c4/Mobile_Legends_Bang_Bang_logo.png",
        "domain": "mobilelegends.com", 
        "id_label": "User ID", 
        "zone": True
    },
    "brawl": {
        "emoji": "🥊", 
        "logo": "https://upload.wikimedia.org/wikipedia/en/8/89/Brawl_Stars_logo.png",
        "domain": "supercell.com", 
        "id_label": "Player Tag (#...)"
    },
    "coc": {
        "emoji": "🏰", 
        "logo": "https://upload.wikimedia.org/wikipedia/commons/b/bc/Clash_of_Clans_logo.png",
        "domain": "supercell.com", 
        "id_label": "Player Tag (#...)"
    },
    "genshin": {
        "emoji": "🌟", 
        "logo": "https://upload.wikimedia.org/wikipedia/en/5/5d/Genshin_Impact_logo.png",
        "domain": "genshin.hoyoverse.com", 
        "id_label": "UID va Server"
    },
    "roblox": {
        "emoji": "🧱", 
        "logo": "https://upload.wikimedia.org/wikipedia/commons/3/3a/Roblox_player_icon_2019.png",
        "domain": "roblox.com", 
        "id_label": "Roblox username"
    },
    "steam": {
        "emoji": "🎮", 
        "logo": "https://upload.wikimedia.org/wikipedia/commons/8/83/Steam_icon_logo.svg.png",
        "domain": "steampowered.com", 
        "id_label": "Steam login"
    },
    "tgprem": {
        "emoji": "💎", 
        "logo": "",
        "domain": "telegram.org", 
        "id_label": "Telegram @username", 
        "kind": "service"
    },
    "tgstars": {
        "emoji": "⭐", 
        "logo": "",
        "domain": "telegram.org", 
        "id_label": "Telegram @username", 
        "kind": "service"
    },
}


# ---------------------------------------------------------------- baza

def db():
    return sqlite3.connect(DB_PATH, timeout=30)


def _ensure_column(cur, table, column, ddl):
    cols = [r[1] for r in cur.execute("PRAGMA table_info(%s)" % table)]
    if column not in cols:
        cur.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl))


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            coins INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            game TEXT,
            package TEXT,
            player_id TEXT,
            coins INTEGER,
            status TEXT DEFAULT 'Kutilmoqda',
            created_at TEXT
        )
    """)
    _ensure_column(cur, "orders", "item_id", "TEXT")
    _ensure_column(cur, "orders", "official_som", "INTEGER DEFAULT 0")
    _ensure_column(cur, "orders", "profit", "INTEGER DEFAULT 0")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments(
            telegram_charge_id TEXT PRIMARY KEY,
            user_id INTEGER,
            coins INTEGER,
            amount INTEGER,
            created_at TEXT
        )
    """)

    con.commit()
    con.close()


def register_user(user):
    con = db()
    con.execute("""
        INSERT OR IGNORE INTO users
        (user_id, username, coins, created_at)
        VALUES (?, ?, 0, ?)
    """, (user.id, user.username or "", datetime.now().isoformat()))
    con.execute(
        "UPDATE users SET username=? WHERE user_id=?",
        (user.username or "", user.id)
    )
    con.commit()
    con.close()


def get_coins(user_id):
    con = db()
    row = con.execute(
        "SELECT coins FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    con.close()
    return row[0] if row else 0


def is_admin(user_id):
    return user_id in ADMIN_IDS


def _ensure_user(cur, user_id, username=""):
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, coins, created_at) "
        "VALUES (?, ?, 0, ?)",
        (user_id, username or "", datetime.now().isoformat())
    )


# ------------------------------------------------- buyurtma mantiqi

def find_item(item_id):
    for g in catalog.shop_items():
        for p in g["packages"]:
            if p["id"] == item_id:
                return {"game_key": g["game"], "game": g["name"], **p}
    return None


def clean_target(game_key, player_id, zone_id):
    meta = GAME_META.get(game_key, {})
    pid = " ".join(str(player_id or "").split())[:64]
    if len(pid) < 3:
        return None, "%s noto'g'ri" % meta.get("id_label", "ID")
    if meta.get("kind") == "service":
        name = pid.lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", name):
            return None, "Telegram username noto'g'ri (masalan: @ali_2000)"
        return "@" + name, None
    if meta.get("zone"):
        zone = str(zone_id or "").strip()
        if not re.fullmatch(r"\d{1,6}", zone):
            return None, "Zone ID noto'g'ri"
        pid = "%s (%s)" % (pid, zone)
    return pid, None


def get_order(cur, order_id):
    row = cur.execute("""
        SELECT o.id, o.user_id, u.username, o.game, o.package, o.player_id,
               o.coins, o.status, COALESCE(o.official_som, 0), COALESCE(o.profit, 0)
        FROM orders o LEFT JOIN users u ON u.user_id = o.user_id
        WHERE o.id=?
    """, (order_id,)).fetchone()
    if not row:
        return None
    keys = ("id", "user_id", "username", "game", "package", "player_id",
            "coins", "status", "official_som", "profit")
    return dict(zip(keys, row))


def create_order(user_id, username, item_id, target):
    if not ADMIN_IDS:
        return False, "Do'kon hali sozlanmagan (ADMIN_IDS yo'q)"
    item = find_item(item_id)
    if not item:
        return False, "Bunday paket yo'q yoki narxi qo'yilmagan"

    con = db()
    try:
        cur = con.cursor()
        _ensure_user(cur, user_id, username)
        cur.execute(
            "UPDATE users SET coins=coins-? WHERE user_id=? AND coins>=?",
            (item["coins"], user_id, item["coins"])
        )
        if cur.rowcount != 1:
            con.rollback()
            return False, "Coin yetarli emas. Avval coin sotib oling."
        cur.execute("""
            INSERT INTO orders
            (user_id, game, package, player_id, coins, status, created_at,
             item_id, official_som, profit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, item["game"], item["label"], target, item["coins"],
              STATUS_PENDING, datetime.now().isoformat(), item_id,
              item["official_som"], item["profit_coin"]))
        order_id = cur.lastrowid
        con.commit()
        return True, get_order(cur, order_id)
    except Exception:
        con.rollback()
        logger.exception("Buyurtma yaratishda xato")
        return False, "Server xatosi"
    finally:
        con.close()


def finish_order(order_id):
    con = db()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE orders SET status=? WHERE id=? AND status=?",
            (STATUS_DONE, order_id, STATUS_PENDING)
        )
        if cur.rowcount != 1:
            con.rollback()
            return None
        order = get_order(cur, order_id)
        if order["profit"] > 0 and PROFIT_USER_ID:
            _ensure_user(cur, PROFIT_USER_ID)
            cur.execute(
                "UPDATE users SET coins=coins+? WHERE user_id=?",
                (order["profit"], PROFIT_USER_ID)
            )
        con.commit()
        return order
    except Exception:
        con.rollback()
        logger.exception("Buyurtmani yakunlashda xato")
        return None
    finally:
        con.close()


def cancel_order(order_id):
    con = db()
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE orders SET status=? WHERE id=? AND status=?",
            (STATUS_CANCEL, order_id, STATUS_PENDING)
        )
        if cur.rowcount != 1:
            con.rollback()
            return None
        order = get_order(cur, order_id)
        cur.execute(
            "UPDATE users SET coins=coins+? WHERE user_id=?",
            (order["coins"], order["user_id"])
        )
        con.commit()
        return order
    except Exception:
        con.rollback()
        logger.exception("Buyurtmani bekor qilishda xato")
        return None
    finally:
        con.close()


def order_text(o):
    who = "👤 %s" % o["user_id"]
    if o.get("username"):
        who += " (@%s)" % o["username"]
    return (
        "🆕 Buyurtma #%s\n%s\n🎮 %s — %s\n🆔 %s\n🪙 %s coin\n"
        "💵 Rasmiy narx: %s so'm\n💰 Foyda: %s coin"
        % (o["id"], who, o["game"], o["package"], o["player_id"],
           o["coins"], format(o["official_som"], ","), o["profit"])
    )


def order_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Bajarildi", callback_data="ord:done:%s" % order_id),
        InlineKeyboardButton(text="❌ Bekor", callback_data="ord:cancel:%s" % order_id),
    ]])


async def send_order_to_admins(order):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id, order_text(order),
                reply_markup=order_keyboard(order["id"])
            )
        except Exception:
            logger.exception("Adminga buyurtma yuborilmadi: %s", admin_id)


def main_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎮 GameDonate Mini App", web_app=WebAppInfo(url=WEBAPP_URL))
    kb.adjust(1)
    return kb.as_markup()


async def notify_admins(text):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.exception("Admin notification failed")


# ------------------------------------------------------ bot buyruqlari

@dp.message(CommandStart())
async def start(message: Message):
    register_user(message.from_user)

    await message.answer(
        "🎮 GAME DONATE\n\n"
        "Xush kelibsiz!\n\n"
        "🪙 1 Coin = 1 000 so'm\n"
        "🎮 O'yin donatlari\n"
        "⭐ Telegram Stars va Premium\n\n"
        "Mini App tugmasini bosing:",
        reply_markup=main_keyboard()
    )


@dp.message(Command("balance"))
async def balance(message: Message):
    register_user(message.from_user)
    coins = get_coins(message.from_user.id)

    await message.answer(
        f"🪙 Balansingiz: {coins} Coin\n"
        f"💰 Qiymati: {coins * COIN_VALUE:,} so'm"
    )


@dp.message(Command("admin"))
async def admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q.")
        return

    con = db()
    users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    orders = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    pending_n = con.execute(
        "SELECT COUNT(*) FROM orders WHERE status=?", (STATUS_PENDING,)
    ).fetchone()[0]
    con.close()

    await message.answer(
        "👑 ADMIN PANEL\n\n"
        f"👥 Foydalanuvchilar: {users}\n"
        f"📦 Buyurtmalar: {orders}\n"
        f"⏳ Kutilayotgan: {pending_n}  (/pending)\n"
        f"💰 Foyda hisobi: {get_coins(PROFIT_USER_ID)} Coin\n\n"
        "Coin berish:\n"
        "/addcoin USER_ID COIN"
    )


@dp.message(Command("pending"))
async def pending(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q.")
        return

    con = db()
    ids = [r[0] for r in con.execute(
        "SELECT id FROM orders WHERE status=? ORDER BY id LIMIT 20",
        (STATUS_PENDING,)
    )]
    orders = [get_order(con.cursor(), i) for i in ids]
    con.close()

    if not orders:
        await message.answer("✅ Kutilayotgan buyurtma yo'q.")
        return

    for o in orders:
        await message.answer(order_text(o), reply_markup=order_keyboard(o["id"]))


@dp.callback_query(F.data.startswith("ord:"))
async def order_action(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q", show_alert=True)
        return

    try:
        _, action, raw_id = call.data.split(":")
        order_id = int(raw_id)
    except ValueError:
        await call.answer("Noto'g'ri tugma")
        return

    if action == "done":
        order = finish_order(order_id)
        mark = "✅ Bajarildi"
    elif action == "cancel":
        order = cancel_order(order_id)
        mark = "❌ Bekor qilindi"
    else:
        await call.answer("Noto'g'ri tugma")
        return

    if not order:
        await call.answer("Bu buyurtma allaqachon ko'rib chiqilgan", show_alert=True)
        return

    await call.answer(mark)

    try:
        await call.message.edit_text(
            "%s\n\n%s — %s" % (call.message.text, mark, call.from_user.full_name)
        )
    except Exception:
        logger.exception("Admin xabarini yangilab bo'lmadi")

    try:
        if action == "done":
            await bot.send_message(
                order["user_id"],
                "✅ Buyurtma #%s bajarildi!\n🎮 %s — %s" % (
                    order["id"], order["game"], order["package"])
            )
        else:
            await bot.send_message(
                order["user_id"],
                "❌ Buyurtma #%s bekor qilindi.\n🪙 %s coin hisobingizga qaytarildi." % (
                    order["id"], order["coins"])
            )
    except Exception:
        logger.exception("Mijozga xabar yuborilmadi")


@dp.message(Command("addcoin"))
async def addcoin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo'q.")
        return

    parts = message.text.split()

    if len(parts) != 3:
        await message.answer("Format: /addcoin USER_ID COIN")
        return

    try:
        user_id = int(parts[1])
        coins = int(parts[2])

        if coins <= 0:
            raise ValueError()

        con = db()
        cur = con.cursor()
        _ensure_user(cur, user_id)
        cur.execute(
            "UPDATE users SET coins=coins+? WHERE user_id=?",
            (coins, user_id)
        )
        con.commit()
        con.close()

        await message.answer(f"✅ {user_id} hisobiga {coins} Coin qo'shildi.")

    except ValueError:
        await message.answer("❌ Ma'lumot noto'g'ri.")


HTML = r"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#080b20">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<title>GameDonate</title>

<style>
*{box-sizing:border-box}
:root{
 --blue:#42c8ff;
 --purple:#a66bff;
 --pink:#ff5edb;
 --glass:rgba(255,255,255,.075);
 --line:rgba(255,255,255,.17);
}
body{
 margin:0;padding:20px 15px 105px;
 min-height:100vh;
 overflow-x:hidden;
 font-family:Arial,sans-serif;
 color:#fff;
 background:
 radial-gradient(ellipse at 15% 10%,#44218b88,transparent 40%),
 radial-gradient(ellipse at 90% 35%,#006caa66,transparent 42%),
 radial-gradient(ellipse at 50% 100%,#601c7666,transparent 45%),
 linear-gradient(145deg,#050817,#10112f 50%,#061a31);
 background-attachment:fixed;
}
body:before,body:after{
 content:"";position:fixed;z-index:-1;
 width:230px;height:230px;border-radius:50%;
 filter:blur(75px);opacity:.34;pointer-events:none;
 animation:floatGlow 13s ease-in-out infinite alternate;
}
body:before{background:#713bff;top:10%;left:-100px}
body:after{background:#00c8ff;right:-110px;bottom:8%;animation-delay:-6s}
@keyframes floatGlow{
 from{transform:translate(0,0)}
 to{transform:translate(65px,-45px)}
}
header{display:flex;justify-content:space-between;align-items:center}
h2{font-size:23px;margin:12px 0}
h3{margin:23px 0 14px}
.glass{
 background:rgba(255,255,255,.075);
 border:1px solid var(--line);
 backdrop-filter:blur(24px);
 -webkit-backdrop-filter:blur(24px);
 border-radius:21px;
 box-shadow:0 10px 35px #0004,inset 0 1px 0 #ffffff18;
}
.balance{padding:21px;margin:20px 0}
.balance small{color:#c9d6ff}
.coins{
 font-size:34px;font-weight:800;margin:10px 0;
 text-shadow:0 0 20px #65caff99;
}
.coin-symbol{
 display:inline-block;
 animation:coinSpin 9s linear infinite;
}
@keyframes coinSpin{to{transform:rotate(360deg)}}
.banner{
 position:relative;overflow:hidden;
 padding:22px;margin:20px 0 25px;
 background:linear-gradient(115deg,#7539d866,#087fd866);
 border:1px solid #ffffff2b;border-radius:23px;
 backdrop-filter:blur(20px);
}
.banner:after{
 content:"";position:absolute;top:-80%;left:-45%;
 width:35%;height:260%;
 transform:rotate(25deg);
 background:linear-gradient(90deg,transparent,#ffffff30,transparent);
 animation:bannerLight 7s infinite;
}
@keyframes bannerLight{
 0%,35%{left:-50%}75%,100%{left:130%}
}
.banner h2{font-size:25px}
.muted{color:#c3c9e7;font-size:13px;line-height:1.5}
.games{display:grid;grid-template-columns:1fr 1fr;gap:13px}
.game{
 min-height:135px;padding:17px 10px;
 display:flex;flex-direction:column;
 justify-content:center;align-items:center;
 text-align:center;gap:10px;
 cursor:pointer;position:relative;overflow:hidden;
 transition:transform .25s,border-color .25s,box-shadow .25s;
}
.game:active{transform:scale(.94)}
.game:hover{border-color:#68cfff99;box-shadow:0 0 22px #388dff33}
.logo{
 width:58px;height:58px;object-fit:contain;
 border-radius:15px;
 filter:drop-shadow(0 4px 12px #0008);
}
.game b{font-size:13px;line-height:1.3}
button{
 position:relative;overflow:hidden;
 border:1px solid #ffffff35;
 background:rgba(255,255,255,.10);
 color:white;border-radius:15px;
 padding:13px 15px;font-weight:bold;
 cursor:pointer;
 transition:transform .18s,box-shadow .25s,border-color .25s;
}
button:active{transform:scale(.94)}
button:before{
 content:"";position:absolute;top:-80%;left:-110%;
 width:55%;height:260%;
 transform:rotate(25deg);
 background:linear-gra
