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

import catalog

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
PROFIT_USER_ID = int(os.getenv("PROFIT_USER_ID", "0") or 0) or (
    ADMIN_IDS[0] if ADMIN_IDS else 0
)

DB_PATH = os.getenv("DB_PATH", "gamedonate.db")
PORT = int(os.getenv("PORT", "8080"))
COIN_VALUE = 1000
STARS_PER_COIN = int(os.getenv("STARS_PER_COIN", "7"))

STATUS_PENDING = "Kutilmoqda"
STATUS_DONE = "Bajarildi"
STATUS_CANCEL = "Bekor qilindi"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GameDonate")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

GAME_META = {
    "pubg": {"emoji": "🎯", "logo": "", "domain": "pubgmobile.com", "id_label": "PUBG Player ID"},
    "mlbb": {"emoji": "⚔️", "logo": "", "domain": "mobilelegends.com", "id_label": "User ID", "zone": True},
    "brawl": {"emoji": "🥊", "logo": "", "domain": "supercell.com", "id_label": "Player Tag (#...)"},
    "coc": {"emoji": "🏰", "logo": "", "domain": "supercell.com", "id_label": "Player Tag (#...)"},
    "genshin": {"emoji": "🌟", "logo": "", "domain": "genshin.hoyoverse.com", "id_label": "UID va Server"},
    "roblox": {"emoji": "🧱", "logo": "", "domain": "roblox.com", "id_label": "Roblox username"},
    "steam": {"emoji": "🎮", "logo": "", "domain": "steampowered.com", "id_label": "Steam login"},
    "tgprem": {"emoji": "💎", "logo": "", "domain": "telegram.org", "id_label": "Telegram @username", "kind": "service"},
    "tgstars": {"emoji": "⭐", "logo": "", "domain": "telegram.org", "id_label": "Telegram @username", "kind": "service"},
}

def db():
    return sqlite3.connect(DB_PATH, timeout=30)

def _ensure_column(cur, table, column, ddl):
    cols = [r[1] for r in cur.execute("PRAGMA table_info(%s)" % table)]
    if column not in cols:
        cur.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl))

def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, coins INTEGER DEFAULT 0, created_at TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, game TEXT, package TEXT, player_id TEXT, coins INTEGER, status TEXT DEFAULT 'Kutilmoqda', created_at TEXT)")
    _ensure_column(cur, "orders", "item_id", "TEXT")
    _ensure_column(cur, "orders", "official_som", "INTEGER DEFAULT 0")
    _ensure_column(cur, "orders", "profit", "INTEGER DEFAULT 0")
    cur.execute("CREATE TABLE IF NOT EXISTS payments(telegram_charge_id TEXT PRIMARY KEY, user_id INTEGER, coins INTEGER, amount INTEGER, created_at TEXT)")
    con.commit()
    con.close()

def register_user(user):
    con = db()
    con.execute("INSERT OR IGNORE INTO users (user_id, username, coins, created_at) VALUES (?, ?, 0, ?)", (user.id, user.username or "", datetime.now().isoformat()))
    con.execute("UPDATE users SET username=? WHERE user_id=?", (user.username or "", user.id))
    con.commit()
    con.close()

def get_coins(user_id):
    con = db()
    row = con.execute("SELECT coins FROM users WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    return row[0] if row else 0

def is_admin(user_id):
    return user_id in ADMIN_IDS

def _ensure_user(cur, user_id, username=""):
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, coins, created_at) VALUES (?, ?, 0, ?)", (user_id, username or "", datetime.now().isoformat()))

def find_item(item_id):
    for g in catalog.shop_items():
        for p in g["packages"]:
            if p["id"] == item_id:
                return {"game_key": g["game"], "game": g["name"], **p}
    return None

def get_order(cur, order_id):
    row = cur.execute("SELECT o.id, o.user_id, u.username, o.game, o.package, o.player_id, o.coins, o.status, COALESCE(o.official_som, 0), COALESCE(o.profit, 0) FROM orders o LEFT JOIN users u ON u.user_id = o.user_id WHERE o.id=?", (order_id,)).fetchone()
    if not row:
        return None
    keys = ("id", "user_id", "username", "game", "package", "player_id", "coins", "status", "official_som", "profit")
    return dict(zip(keys, row))

def finish_order(order_id):
    con = db()
    try:
        cur = con.cursor()
        cur.execute("UPDATE orders SET status=? WHERE id=? AND status=?", (STATUS_DONE, order_id, STATUS_PENDING))
        if cur.rowcount != 1:
            con.rollback()
            return None
        order = get_order(cur, order_id)
        if order["profit"] > 0 and PROFIT_USER_ID:
            _ensure_user(cur, PROFIT_USER_ID)
            cur.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (order["profit"], PROFIT_USER_ID))
        con.commit()
        return order
    except Exception:
        con.rollback()
        return None
    finally:
        con.close()

def cancel_order(order_id):
    con = db()
    try:
        cur = con.cursor()
        cur.execute("UPDATE orders SET status=? WHERE id=? AND status=?", (STATUS_CANCEL, order_id, STATUS_PENDING))
        if cur.rowcount != 1:
            con.rollback()
            return None
        order = get_order(cur, order_id)
        cur.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (order["coins"], order["user_id"]))
        con.commit()
        return order
    except Exception:
        con.rollback()
        return None
    finally:
        con.close()

def order_text(o):
    who = "👤 %s" % o["user_id"]
    if o.get("username"):
        who += " (@%s)" % o["username"]
    return "🆕 Buyurtma #%s\n%s\n🎮 %s — %s\n🆔 %s\n🪙 %s coin\n💵 Rasmiy narx: %s so'm\n💰 Foyda: %s coin" % (o["id"], who, o["game"], o["package"], o["player_id"], o["coins"], format(o["official_som"], ","), o["profit"])

def order_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Bajarildi", callback_data="ord:done:%s" % order_id),
        InlineKeyboardButton(text="❌ Bekor", callback_data="ord:cancel:%s" % order_id),
    ]])

def main_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎮 GameDonate Mini App", web_app=WebAppInfo(url=WEBAPP_URL))
    kb.adjust(1)
    return kb.as_markup()

@dp.message(CommandStart())
async def start(message: Message):
    register_user(message.from_user)
    await message.answer("🎮 GAME DONATE\n\nXush kelibsiz!\n\n🪙 1 Coin = 1 000 so'm\n🎮 O'yin donatlari\n⭐ Telegram Stars va Premium\n\nMini App tugmasini bosing:", reply_markup=main_keyboard())

@dp.message(Command("balance"))
async def balance(message: Message):
    register_user(message.from_user)
    coins = get_coins(message.from_user.id)
    await message.answer(f"🪙 Balansingiz: {coins} Coin\n💰 Qiymati: {coins * COIN_VALUE:,} so'm")

@dp.message(Command("admin"))
async def admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    con = db()
    users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    orders = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    pending_n = con.execute("SELECT COUNT(*) FROM orders WHERE status=?", (STATUS_PENDING,)).fetchone()[0]
    con.close()
    await message.answer(f"👑 ADMIN PANEL\n\n👥 Foydalanuvchilar: {users}\n📦 Buyurtmalar: {orders}\n⏳ Kutilayotgan: {pending_n}  (/pending)\n💰 Foyda hisobi: {get_coins(PROFIT_USER_ID)} Coin")

@dp.callback_query(F.data.startswith("ord:"))
async def order_action(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    try:
        _, action, raw_id = call.data.split(":")
        order_id = int(raw_id)
    except ValueError:
        return
    if action == "done":
        order = finish_order(order_id)
        mark = "✅ Bajarildi"
    else:
        order = cancel_order(order_id)
        mark = "❌ Bekor qilindi"
    if order:
        await call.answer(mark)
        try:
            await call.message.edit_text("%s\n\n%s" % (call.message.text, mark))
        except Exception:
            pass

app = Flask(__name__)

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
 background:linear-gradient(90deg,transparent,#ffffff30,transparent);
 animation:btnLight 4s infinite;
}
@keyframes btnLight{
 0%,40%{left:-130%}80%,100%{left:130%}
}
</style>
</head>
<body>
  <header>
    <h2>GameDonate</h2>
  </header>
  <div class="glass balance">
    <small>Balans</small>
    <div class="coins"><span class="coin-symbol">🪙</span> 0</div>
  </div>
  <div class="banner">
    <h2>Xush kelibsiz!</h2>
    <p class="muted">PUBG Mobile UC va Mobile Legends olmoslarini qulay narxlarda xarid qiling.</p>
  </div>
  <h3>O'yinni tanlang</h3>
  <div class="games">
    <div class="game glass">
      <b>PUBG Mobile UC</b>
    </div>
    <div class="game glass">
      <b>MLBB Diamonds</b>
    </div>
  </div>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

async def run_bot():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    import threading
    threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
