import asyncio, os, io, csv, time, sqlite3
from aiogram import Bot, Dispatcher, F
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, BufferedInputFile, FSInputFile)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command, StateFilter

# ============ CONFIG ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPER_ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SHOP_NAME = os.getenv("SHOP_NAME", "Rembo Shop")
SHOP_EMOJI = "🦝"
REMINDER_MIN = int(os.getenv("REMINDER_MINUTES", "10"))
DB_PATH = os.getenv("DB_PATH", "/data/shop.db")
if not os.path.exists("/data"): DB_PATH = "shop.db"

# ============ DB ============
d = os.path.dirname(DB_PATH)
if d: os.makedirs(d, exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

cur.executescript("""
CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY AUTOINCREMENT,
 country TEXT NOT NULL, phone TEXT, price INTEGER NOT NULL, status TEXT DEFAULT 'available');
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER, account_id INTEGER, status TEXT DEFAULT 'pending',
 paid_at INTEGER, number_issued_at INTEGER, reminded_number INTEGER DEFAULT 0,
 reminded_code INTEGER DEFAULT 0, receipt_admin_msg_id INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER, admin_msg_id INTEGER, status TEXT DEFAULT 'open', order_id INTEGER);
CREATE TABLE IF NOT EXISTS ticket_msgs(id INTEGER PRIMARY KEY AUTOINCREMENT,
 ticket_id INTEGER, admin_msg_id INTEGER);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY,
 username TEXT, added_by INTEGER, added_at INTEGER);
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT,
 first_seen INTEGER, last_seen INTEGER, blocked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS templates(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT);
""")
conn.commit()

for tbl, col, ct in [
    ("orders","paid_at","INTEGER"),("orders","number_issued_at","INTEGER"),
    ("orders","reminded_number","INTEGER DEFAULT 0"),("orders","reminded_code","INTEGER DEFAULT 0"),
    ("orders","receipt_admin_msg_id","INTEGER"),("orders","note","TEXT"),
    ("tickets","order_id","INTEGER"),
]:
    try: cur.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ct}")
    except sqlite3.OperationalError: pass
conn.commit()

def get_setting(k, default=None):
    cur.execute("SELECT value FROM settings WHERE key=?", (k,)); r = cur.fetchone()
    return r[0] if r else default
def set_setting(k, v):
    cur.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v)); conn.commit()

for k, deflt in [("pay_phone", os.getenv("PAYMENT_PHONE", "+7 999 123 45 67")),
                  ("pay_bank", os.getenv("PAYMENT_BANK", "Т-Банк")),
                  ("pay_name", os.getenv("PAYMENT_NAME", "Иван И."))]:
    if get_setting(k) is None: set_setting(k, deflt)

cur.execute("INSERT OR IGNORE INTO admins(user_id,username,added_by,added_at) VALUES(?,?,?,?)",
            (SUPER_ADMIN_ID, "super", SUPER_ADMIN_ID, int(time.time()))); conn.commit()

# ============ HELPERS ============
def is_admin(uid):
    if uid == SUPER_ADMIN_ID: return True
    cur.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)); return cur.fetchone() is not None

def track_user(uid, uname):
    now = int(time.time())
    cur.execute("SELECT 1 FROM users WHERE user_id=?", (uid,))
    if cur.fetchone(): cur.execute("UPDATE users SET username=?, last_seen=? WHERE user_id=?", (uname, now, uid))
    else: cur.execute("INSERT INTO users(user_id,username,first_seen,last_seen) VALUES(?,?,?,?)", (uid,uname,now,now))
    conn.commit()

def is_blocked(uid):
    cur.execute("SELECT blocked FROM users WHERE user_id=?", (uid,)); r=cur.fetchone()
    return bool(r and r[0]==1)

def get_countries():
    cur.execute("SELECT country,MIN(price) FROM accounts WHERE status='available' GROUP BY country"); return cur.fetchall()
def get_acc_by_country(c):
    cur.execute("SELECT id,price FROM accounts WHERE country=? AND status='available'", (c,)); return cur.fetchall()
def get_acc(i):
    cur.execute("SELECT * FROM accounts WHERE id=?", (i,)); return cur.fetchone()
def admin_add_acc(c,p):
    cur.execute("INSERT INTO accounts(country,price) VALUES(?,?)",(c,p)); conn.commit(); return cur.lastrowid
def admin_del_acc(i):
    cur.execute("DELETE FROM accounts WHERE id=? AND status='available'",(i,)); conn.commit()
def admin_upd_acc(i, c, p):
    cur.execute("UPDATE accounts SET country=?,price=? WHERE id=?", (c,p,i)); conn.commit()
def save_phone(i, ph):
    cur.execute("UPDATE accounts SET phone=? WHERE id=?", (ph,i)); conn.commit()

def create_order(uid, aid):
    cur.execute("INSERT INTO orders(user_id,account_id) VALUES(?,?)",(uid,aid)); conn.commit(); return cur.lastrowid
def get_order(i):
    cur.execute("SELECT * FROM orders WHERE id=?", (i,)); return cur.fetchone()
def upd_status(i, status, extra=""):
    q = f"UPDATE orders SET status=?{extra} WHERE id=?"
    params = [status]
    if "paid_at" in extra: params.append(int(time.time()))
    if "number_issued_at" in extra: params.append(int(time.time()))
    params.append(i)
    cur.execute(q, params); conn.commit()
def mark_paid(i): upd_status(i, "paid", ",paid_at=?")
def mark_number(i): upd_status(i, "number_issued", ",number_issued_at=?")
def mark_issued(i):
    upd_status(i, "issued")
    cur.execute("UPDATE accounts SET status='sold' WHERE id=(SELECT account_id FROM orders WHERE id=?)",(i,)); conn.commit()
def mark_rejected(i): upd_status(i, "rejected")
def mark_refunded(i):
    upd_status(i, "refunded")
    cur.execute("UPDATE accounts SET status='available', phone=NULL WHERE id=(SELECT account_id FROM orders WHERE id=?)",(i,)); conn.commit()
def mark_receipt(i): cur.execute("UPDATE orders SET status='receipt_sent' WHERE id=?", (i,)); conn.commit()
def order_note(i, note):
    cur.execute("UPDATE orders SET note=? WHERE id=?", (note,i)); conn.commit()

def get_user_stats(uid):
    cur.execute("SELECT COUNT(*), COALESCE(SUM(a.price),0) FROM orders o JOIN accounts a ON o.account_id=a.id WHERE o.user_id=? AND o.status IN ('paid','number_issued','issued')", (uid,))
    return cur.fetchone()

def add_admin(uid, uname, by):
    cur.execute("INSERT OR IGNORE INTO admins(user_id,username,added_by,added_at) VALUES(?,?,?,?)", (uid,uname,by,int(time.time()))); conn.commit()
def rm_admin(uid):
    if uid == SUPER_ADMIN_ID: return False
    cur.execute("DELETE FROM admins WHERE user_id=?", (uid,)); conn.commit(); return True
def list_admins():
    cur.execute("SELECT user_id,username,added_at FROM admins ORDER BY added_at"); return cur.fetchall()

def add_tmpl(t): cur.execute("INSERT INTO templates(text) VALUES(?)",(t,)); conn.commit()
def list_tmpls(): cur.execute("SELECT id,text FROM templates"); return cur.fetchall()
def del_tmpl(i): cur.execute("DELETE FROM templates WHERE id=?", (i,)); conn.commit()

def stats():
    cur.execute("SELECT COUNT(*) FROM accounts WHERE status='available'"); a = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM accounts WHERE status='sold'"); s = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(a.price),0) FROM orders o JOIN accounts a ON o.account_id=a.id WHERE o.status IN ('paid','number_issued','issued')"); rev = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders WHERE status='receipt_sent'"); rc = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders WHERE status='paid'"); p = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders WHERE status='number_issued'"); n = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users"); u = cur.fetchone()[0]
    now = int(time.time())
    cur.execute("SELECT COALESCE(SUM(a.price),0) FROM orders o JOIN accounts a ON o.account_id=a.id WHERE o.status IN ('paid','number_issued','issued') AND o.paid_at > ?", (now-86400,)); d1 = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(a.price),0) FROM orders o JOIN accounts a ON o.account_id=a.id WHERE o.status IN ('paid','number_issued','issued') AND o.paid_at > ?", (now-7*86400,)); d7 = cur.fetchone()[0]
    return {"available":a,"sold":s,"revenue":rev,"receipt":rc,"paid":p,"number":n,"users":u,"day":d1,"week":d7}

async def notify(uid, text, **kw):
    try: await bot.send_message(uid, text, **kw)
    except Exception as e: print(f"notify error {uid}: {e}")

# ============ KEYBOARDS ============
def main_menu(uid):
    kb = [
        [InlineKeyboardButton(text="🛒 Купить аккаунт", callback_data="shop")],
        [InlineKeyboardButton(text="📋 Мои заказы", callback_data="my_orders")],
        [InlineKeyboardButton(text="📖 Как это работает", callback_data="info")],
        [InlineKeyboardButton(text="💬 Поддержка", callback_data="support_start")],
    ]
    if is_admin(uid):
        kb.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def back_main():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{SHOP_EMOJI} В меню", callback_data="back_main")]])

def countries_kb(c):
    kb = [[InlineKeyboardButton(text=f"🌍 {x} — от {y}₽", callback_data=f"country:{x}")] for x,y in c]
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def numbers_kb(c, n):
    kb = [[InlineKeyboardButton(text=f"📱 Аккаунт — {p}₽", callback_data=f"buy:{i}")] for i,p in n]
    kb.append([InlineKeyboardButton(text="⬅️ К странам", callback_data="shop")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def payment_kb(oid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил (скрин)", callback_data=f"send_receipt:{oid}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="back_main")]])

def receipt_confirm_kb(oid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin_confirm_pay:{oid}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject_pay:{oid}")]])

def my_orders_kb(orders):
    kb = []
    for oid,status,country,price in orders:
        e = {"pending":"⏳","receipt_sent":"🧾","paid":"💰","number_issued":"📱","issued":"✅","rejected":"❌","refunded":"↩️"}.get(status,"❓")
        kb.append([InlineKeyboardButton(text=f"{e} №{oid} | {country} | {price}₽", callback_data=f"my_order:{oid}")])
    kb.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="a_stats")],
        [InlineKeyboardButton(text="📱 Аккаунты", callback_data="a_accs:0")],
        [InlineKeyboardButton(text="🧾 Заказы", callback_data="a_orders")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="a_users:0")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="a_broadcast")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="a_settings")],
        [InlineKeyboardButton(text="👑 Админы", callback_data="a_admins")],
        [InlineKeyboardButton(text="💾 Бэкап БД", callback_data="a_backup")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")]])

def a_back(to="admin_menu"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=to)]])

def a_accs_kb(accs, page, total):
    kb = []
    for aid,c,ph,p,st in accs:
        e = "✅" if st=="available" else "💸"
        ph_ = ph if ph else "🔒"
        kb.append([InlineKeyboardButton(text=f"{e} {c} | {ph_} | {p}₽", callback_data=f"a_acc:{aid}")])
    nav = []
    if page>0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"a_accs:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}", callback_data="noop"))
    if (page+1)*10 < total: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"a_accs:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton(text="➕ Добавить", callback_data="a_add"),
               InlineKeyboardButton(text="📦 Массово", callback_data="a_mass")])
    kb.append([InlineKeyboardButton(text="📥 CSV", callback_data="a_csv"),
               InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def a_acc_actions(aid, status):
    kb = []
    if status == "available":
        kb.append([InlineKeyboardButton(text="✏️ Изменить", callback_data=f"a_edit:{aid}")])
        kb.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"a_del:{aid}")])
    kb.append([InlineKeyboardButton(text="⬅️ К аккаунтам", callback_data="a_accs:0")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def a_orders_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧾 Ждут проверки", callback_data="a_of:receipt_sent:0")],
        [InlineKeyboardButton(text="💰 Оплачены", callback_data="a_of:paid:0")],
        [InlineKeyboardButton(text="📱 Ждут кода", callback_data="a_of:number_issued:0")],
        [InlineKeyboardButton(text="✅ Завершены", callback_data="a_of:issued:0")],
        [InlineKeyboardButton(text="❌ Отклонены", callback_data="a_of:rejected:0")],
        [InlineKeyboardButton(text="📋 Все", callback_data="a_of:all:0")],
        [InlineKeyboardButton(text="🔍 Поиск", callback_data="a_search")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_menu")]])

def a_orders_kb(orders, status, page, total):
    kb = []
    emo = {"pending":"⏳","receipt_sent":"🧾","paid":"💰","number_issued":"📱","issued":"✅","rejected":"❌","refunded":"↩️"}
    for oid,uid,st,c,ph,p in orders:
        kb.append([InlineKeyboardButton(text=f"{emo.get(st,'❓')} №{oid} | {c} | {p}₽ | {uid}", callback_data=f"a_oi:{oid}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"a_of:{status}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}", callback_data="noop"))
    if (page+1)*10 < total: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"a_of:{status}:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton(text="⬅️ К фильтрам", callback_data="a_orders")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def a_order_info_kb(oid, status):
    kb=[]
    if status=="receipt_sent":
        kb.append([InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin_confirm_pay:{oid}")])
        kb.append([InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject_pay:{oid}")])
    elif status=="paid":
        kb.append([InlineKeyboardButton(text="📱 Выдать номер", callback_data=f"a_give_phone:{oid}")])
    elif status=="number_issued":
        kb.append([InlineKeyboardButton(text="🔑 Выдать код", callback_data=f"a_give_code:{oid}")])
    if status in ("paid","number_issued","issued"):
        kb.append([InlineKeyboardButton(text="↩️ Возврат", callback_data=f"a_refund:{oid}")])
    kb.append([InlineKeyboardButton(text="📝 Заметка", callback_data=f"a_note:{oid}")])
    kb.append([InlineKeyboardButton(text="⬅️ К заказам", callback_data="a_orders")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def a_users_kb(users, page, total):
    kb=[]
    for uid,uname,bl in users:
        e = "🚫" if bl else "👤"
        kb.append([InlineKeyboardButton(text=f"{e} {uname or uid} | ID {uid}", callback_data=f"a_user:{uid}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"a_users:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}", callback_data="noop"))
    if (page+1)*10 < total: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"a_users:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def a_user_kb(uid, blocked):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Разблокировать" if blocked else "🚫 Заблокировать", callback_data=f"a_block:{uid}")],
        [InlineKeyboardButton(text="⬅️ К юзерам", callback_data="a_users:0")]])

def a_admins_kb(admins):
    kb=[]
    for uid,uname,_ in admins:
        label = f"👑 {uname or uid} (ID {uid})"
        kb.append([InlineKeyboardButton(text=label, callback_data=f"a_rm_admin:{uid}")])
    kb.append([InlineKeyboardButton(text="➕ Добавить админа", callback_data="a_add_admin")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def a_settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 Телефон СБП", callback_data="a_set_phone")],
        [InlineKeyboardButton(text="🏦 Банк", callback_data="a_set_bank")],
        [InlineKeyboardButton(text="👤 Получатель", callback_data="a_set_name")],
        [InlineKeyboardButton(text="📝 Шаблоны ответов", callback_data="a_tmpls")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_menu")]])

# ============ STATES ============
class S(StatesGroup):
    add_country=State(); add_price=State(); add_mass=State(); add_edit_c=State()
    give_phone=State(); give_code=State(); note=State()
    receipt=State(); support=State(); broadcast=State(); search=State()
    set_pay_phone=State(); set_pay_bank=State(); set_pay_name=State()
    tmpl_add=State(); add_admin=State()

# ============ BOT ============
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ============ USER ============
@dp.message(Command("start"))
async def start(m: Message):
    track_user(m.from_user.id, m.from_user.username)
    if is_blocked(m.from_user.id): return await m.answer("🚫 Вы заблокированы.")
    await m.answer(f"{SHOP_EMOJI} <b>Добро пожаловать в {SHOP_NAME}!</b>\n\n"
        f"🔹 Покупка аккаунтов с номерами\n🔹 Оплата по СБП\n🔹 Номер и код — после оплаты\n\n"
        f"Выберите действие 👇", reply_markup=main_menu(m.from_user.id), parse_mode="HTML")

@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id): return await m.answer("⛔ Нет доступа.")
    await m.answer(f"⚙️ <b>{SHOP_NAME} — Админ</b>\n\nВыберите раздел:", reply_markup=admin_menu(), parse_mode="HTML")

@dp.callback_query(F.data == "back_main")
async def bk(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(f"{SHOP_EMOJI} <b>Меню</b>", reply_markup=main_menu(c.from_user.id), parse_mode="HTML")

@dp.callback_query(F.data == "noop")
async def noop(c: CallbackQuery): await c.answer()

@dp.callback_query(F.data == "shop")
async def shop(c: CallbackQuery):
    cs = get_countries()
    if not cs: return await c.message.edit_text("😔 Нет аккаунтов. Загляните позже.", reply_markup=back_main())
    await c.message.edit_text("🌍 <b>Выберите страну:</b>\n\n🔒 Номер — после оплаты.", reply_markup=countries_kb(cs), parse_mode="HTML")

@dp.callback_query(F.data.startswith("country:"))
async def cnt(c: CallbackQuery):
    ctry = c.data.split(":",1)[1]
    ns = get_acc_by_country(ctry)
    if not ns: return await c.answer("Нет аккаунтов", show_alert=True)
    await c.message.edit_text(f"🌍 <b>{ctry}</b>\n\n📱 Доступные аккаунты:", reply_markup=numbers_kb(ctry, ns), parse_mode="HTML")

@dp.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    if is_blocked(c.from_user.id): return await c.answer("Вы заблокированы", show_alert=True)
    aid = int(c.data.split(":")[1]); a = get_acc(aid)
    if not a or a[4]!="available": return await c.answer("Уже продан", show_alert=True)
    oid = create_order(c.from_user.id, aid); _,ctry,ph,price,_ = a
    await c.message.edit_text(
        f"🧾 <b>Заказ №{oid} — {SHOP_NAME}</b>\n\n"
        f"🌍 Страна: <b>{ctry}</b>\n📱 Номер: <b>🔒 после оплаты</b>\n💰 Сумма: <b>{price}₽</b>\n\n"
        f"💳 <b>Реквизиты (СБП):</b>\n\n"
        f"📞 Телефон: <code>{get_setting('pay_phone')}</code>\n"
        f"🏦 Банк: <b>{get_setting('pay_bank')}</b>\n"
        f"👤 Получатель: <b>{get_setting('pay_name')}</b>\n\n"
        f"⚠️ Переведите ровно <b>{price}₽</b> и сохраните чек.\n\n"
        f"После перевода нажмите кнопку и пришлите скриншот.",
        reply_markup=payment_kb(oid), parse_mode="HTML")

@dp.callback_query(F.data.startswith("send_receipt:"))
async def send_receipt(c: CallbackQuery, state: FSMContext):
    oid = int(c.data.split(":")[1]); o = get_order(oid)
    if not o or o[3]!="pending": return await c.answer("Заказ уже обработан", show_alert=True)
    await state.set_state(S.receipt); await state.update_data(oid=oid)
    await c.message.edit_text(f"📸 <b>Отправьте скриншот перевода</b>\n\nЗаказ №{oid}\n\nПришлите фото или файл.", reply_markup=back_main(), parse_mode="HTML")

@dp.message(S.receipt, F.photo | F.document)
async def receipt_get(m: Message, state: FSMContext):
    data = await state.get_data(); oid = data["oid"]; o = get_order(oid)
    if not o: await state.clear(); return await m.answer("Заказ не найден")
    a = get_acc(o[2]); u = m.from_user
    mark_receipt(oid)
    for adm_id,_,_ in list_admins():
        try:
            await bot.send_message(adm_id, f"🧾 <b>Скрин — Заказ №{oid}</b>\n\n👤 @{u.username or u.id}\n🆔 <code>{u.id}</code>\n🌍 {a[1]}\n💰 {a[3]}₽", parse_mode="HTML")
            await m.copy_to(adm_id)
            await bot.send_message(adm_id, f"Заказ №{oid}", reply_markup=receipt_confirm_kb(oid))
        except Exception as e: print(f"send receipt err: {e}")
    await state.clear()
    await m.answer(f"✅ <b>Скриншот отправлен!</b>\n\nЗаказ №{oid} на проверке. Ожидайте.", reply_markup=back_main(), parse_mode="HTML")

@dp.message(S.receipt)
async def receipt_wrong(m: Message): await m.answer("❌ Пришлите <b>фото</b> или <b>файл</b>.", parse_mode="HTML")

@dp.callback_query(F.data == "info")
async def info(c: CallbackQuery):
    await c.message.edit_text(
        f"📖 <b>Как работает {SHOP_NAME}:</b>\n\n"
        f"1️⃣ Выбираете страну\n2️⃣ Выбираете аккаунт\n3️⃣ Переводите по СБП\n"
        f"4️⃣ Присылаете скриншот\n5️⃣ Админ подтверждает\n6️⃣ Получаете номер, затем код\n\n"
        f"🔒 Номер скрыт до оплаты.", reply_markup=back_main(), parse_mode="HTML")

# ============ MY ORDERS ============
@dp.callback_query(F.data == "my_orders")
async def my_orders(c: CallbackQuery):
    cur.execute("""SELECT o.id,o.status,a.country,a.price FROM orders o
        JOIN accounts a ON o.account_id=a.id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 20""", (c.from_user.id,))
    rows = cur.fetchall()
    if not rows: return await c.message.edit_text("📋 У вас пока нет заказов.", reply_markup=back_main())
    await c.message.edit_text("📋 <b>Ваши заказы:</b>", reply_markup=my_orders_kb(rows), parse_mode="HTML")

@dp.callback_query(F.data.startswith("my_order:"))
async def my_order(c: CallbackQuery):
    oid = int(c.data.split(":")[1]); o = get_order(oid)
    if not o or o[1]!=c.from_user.id: return await c.answer("Не найден", show_alert=True)
    a = get_acc(o[2])
    emo = {"pending":"⏳ Ожидает оплаты","receipt_sent":"🧾 На проверке","paid":"💰 Оплачен (ждём номер)",
           "number_issued":"📱 Номер выдан (ждём код)","issued":"✅ Завершён","rejected":"❌ Отклонён","refunded":"↩️ Возврат"}
    text = f"🧾 <b>Заказ №{oid}</b>\n\n🌍 {a[1]}\n💰 {a[3]}₽\n📌 Статус: <b>{emo.get(o[3], o[3])}</b>"
    if a[2] and o[3] in ("number_issued","issued"): text += f"\n\n📞 Номер: <code>{a[2]}</code>"
    kb = [[InlineKeyboardButton(text="🆘 Проблема с заказом", callback_data=f"order_problem:{oid}")],
          [InlineKeyboardButton(text="⬅️ К заказам", callback_data="my_orders")]]
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

@dp.callback_query(F.data.startswith("order_problem:"))
async def order_problem(c: CallbackQuery, state: FSMContext):
    oid = int(c.data.split(":")[1])
    await state.set_state(S.support); await state.update_data(order_id=oid)
    await c.message.edit_text(f"🆘 Опишите проблему по заказу №{oid} одним сообщением.", reply_markup=back_main())

# ============ SUPPORT ============
@dp.callback_query(F.data == "support_start")
async def sup_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.support); await state.update_data(order_id=None)
    await c.message.edit_text(f"💬 <b>Поддержка {SHOP_NAME}</b>\n\nНапишите вопрос одним сообщением.", reply_markup=back_main(), parse_mode="HTML")

@dp.message(S.support)
async def sup_recv(m: Message, state: FSMContext):
    data = await state.get_data(); oid = data.get("order_id")
    await state.clear()
    cur.execute("INSERT INTO tickets(user_id,order_id) VALUES(?,?)", (m.from_user.id, oid)); conn.commit()
    tid = cur.lastrowid
    for adm_id,_,_ in list_admins():
        try:
            ctx = f"\n🧾 Заказ №{oid}" if oid else ""
            await bot.send_message(adm_id, f"🆘 <b>Тикет #{tid}</b>\n👤 @{m.from_user.username or m.from_user.id}{ctx}\n🆔 <code>{m.from_user.id}</code>\n\n↩️ Отвечайте reply:", parse_mode="HTML")
            cp = await m.copy_to(adm_id)
            cur.execute("INSERT INTO ticket_msgs(ticket_id,admin_msg_id) VALUES(?,?)", (tid, cp.message_id)); conn.commit()
            await bot.send_message(adm_id, f"Тикет #{tid}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔒 Закрыть", callback_data=f"a_close_t:{tid}")]]))
        except Exception as e: print(f"ticket err: {e}")
    await m.answer(f"✅ Обращение #{tid} отправлено.", reply_markup=back_main())

@dp.message(F.reply_to_message, StateFilter(None))
async def reply_handler(m: Message):
    if not is_admin(m.from_user.id): return
    cur.execute("SELECT t.user_id FROM tickets t JOIN ticket_msgs tm ON tm.ticket_id=t.id WHERE tm.admin_msg_id=?", (m.reply_to_message.message_id,))
    r = cur.fetchone()
    if not r: return
    try:
        await m.copy_to(r[0]); await m.answer("✅ Отправлено.")
    except Exception as e: await m.answer(f"❌ {e}")

# ============ ADMIN ============
@dp.callback_query(F.data == "admin_menu")
async def a_menu(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа", show_alert=True)
    await state.clear()
    await c.message.edit_text("⚙️ <b>Админ-панель</b>", reply_markup=admin_menu(), parse_mode="HTML")

@dp.callback_query(F.data == "a_stats")
async def a_st(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    s = stats()
    await c.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"🟢 Доступно: <b>{s['available']}</b>\n💸 Продано: <b>{s['sold']}</b>\n"
        f"🧾 Ждут проверки: <b>{s['receipt']}</b>\n💰 Оплачены: <b>{s['paid']}</b>\n"
        f"📱 Ждут кода: <b>{s['number']}</b>\n👥 Пользователей: <b>{s['users']}</b>\n\n"
        f"💵 Выручка сегодня: <b>{s['day']}₽</b>\n"
        f"💵 За 7 дней: <b>{s['week']}₽</b>\n"
        f"💵 Всего: <b>{s['revenue']}₽</b>",
        reply_markup=a_back(), parse_mode="HTML")

# --- accounts ---
@dp.callback_query(F.data.startswith("a_accs:"))
async def a_accs(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    pg = int(c.data.split(":")[1])
    cur.execute("SELECT COUNT(*) FROM accounts"); tot = cur.fetchone()[0]
    cur.execute("SELECT id,country,phone,price,status FROM accounts ORDER BY id DESC LIMIT 10 OFFSET ?", (pg*10,))
    accs = cur.fetchall()
    if not accs: return await c.message.edit_text("📱 Пусто. Добавьте аккаунт.", reply_markup=admin_menu())
    await c.message.edit_text(f"📱 <b>Аккаунты ({tot})</b>", reply_markup=a_accs_kb(accs, pg, tot), parse_mode="HTML")

@dp.callback_query(F.data.startswith("a_acc:"))
async def a_acc(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    aid = int(c.data.split(":")[1]); a = get_acc(aid)
    if not a: return await c.answer("Не найден", show_alert=True)
    await c.message.edit_text(
        f"📱 <b>Аккаунт #{aid}</b>\n\n🌍 {a[1]}\n📞 {a[2] or '🔒'}\n💰 {a[3]}₽\n📌 {a[4]}",
        reply_markup=a_acc_actions(aid, a[4]), parse_mode="HTML")

@dp.callback_query(F.data.startswith("a_del:"))
async def a_del(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    admin_del_acc(int(c.data.split(":")[1]))
    await c.answer("Удалён")
    cur.execute("SELECT COUNT(*) FROM accounts"); tot = cur.fetchone()[0]
    cur.execute("SELECT id,country,phone,price,status FROM accounts ORDER BY id DESC LIMIT 10 OFFSET 0")
    await c.message.edit_text(f"📱 <b>Аккаунты ({tot})</b>\n\n🗑 Удалено", reply_markup=a_accs_kb(cur.fetchall(), 0, tot), parse_mode="HTML")

@dp.callback_query(F.data.startswith("a_edit:"))
async def a_edit(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    aid = int(c.data.split(":")[1])
    await state.update_data(eid=aid); await state.set_state(S.add_edit_c)
    await c.message.edit_text("✏️ Введите новые данные:\n\n<code>Страна, Цена</code>\n\nПример: <code>Казахстан, 150</code>", parse_mode="HTML")

@dp.message(S.add_edit_c)
async def a_edit_save(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    try: c_, p_ = [x.strip() for x in m.text.split(",")]; p_ = int(p_)
    except: return await m.answer("❌ Формат: <code>Страна, Цена</code>", parse_mode="HTML")
    data = await state.get_data(); admin_upd_acc(data["eid"], c_, p_); await state.clear()
    await m.answer(f"✅ Обновлён #{data['eid']}", reply_markup=admin_menu())

@dp.callback_query(F.data == "a_add")
async def a_add(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    await state.set_state(S.add_country)
    await c.message.edit_text("➕ <b>Добавить аккаунт</b>\n\nШаг 1/2: Страна", reply_markup=a_back(), parse_mode="HTML")

@dp.message(S.add_country)
async def a_add_c(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    if len(m.text.strip())<2: return await m.answer("Слишком коротко")
    await state.update_data(c=m.text.strip()); await state.set_state(S.add_price)
    await m.answer(f"✅ Страна: <b>{m.text.strip()}</b>\n\nШаг 2/2: Цена (₽)", parse_mode="HTML")

@dp.message(S.add_price)
async def a_add_p(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    try: p = int(m.text.strip())
    except: return await m.answer("❌ Число")
    data = await state.get_data(); admin_add_acc(data["c"], p); await state.clear()
    await m.answer(f"✅ Добавлен: {data['c']} — {p}₽", reply_markup=admin_menu())

@dp.callback_query(F.data == "a_mass")
async def a_mass(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    await state.set_state(S.add_mass)
    await c.message.edit_text("📦 <b>Массовое добавление</b>\n\nОдна строка = один аккаунт:\n\n<code>Казахстан, 150\nРоссия, 200\nУкраина, 180</code>", reply_markup=a_back(), parse_mode="HTML")

@dp.message(S.add_mass)
async def a_mass_save(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    added = 0; errors = 0
    for line in m.text.strip().split("\n"):
        try:
            c_, p_ = [x.strip() for x in line.split(",")]; admin_add_acc(c_, int(p_)); added += 1
        except: errors += 1
    await state.clear()
    await m.answer(f"✅ Добавлено: {added}\n❌ Ошибок: {errors}", reply_markup=admin_menu())

@dp.callback_query(F.data == "a_csv")
async def a_csv(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    cur.execute("SELECT id,country,phone,price,status FROM accounts")
    rows = cur.fetchall()
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(["id","country","phone","price","status"]); w.writerows(rows)
    file = BufferedInputFile(buf.getvalue().encode("utf-8"), filename="accounts.csv")
    await c.message.answer_document(file, caption="📥 CSV аккаунтов")

@dp.callback_query(F.data == "a_backup")
async def a_backup(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    if os.path.exists(DB_PATH):
        await c.message.answer_document(FSInputFile(DB_PATH), caption=f"💾 Бэкап {DB_PATH}")

# --- orders ---
@dp.callback_query(F.data == "a_orders")
async def a_orders(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    await c.message.edit_text("🧾 <b>Заказы — фильтр:</b>", reply_markup=a_orders_menu(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("a_of:"))
async def a_of(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    _, st, pg = c.data.split(":"); pg = int(pg)
    if st == "all":
        cur.execute("SELECT COUNT(*) FROM orders")
    else:
        cur.execute("SELECT COUNT(*) FROM orders WHERE status=?", (st,))
    tot = cur.fetchone()[0]
    q = """SELECT o.id,o.user_id,o.status,a.country,a.phone,a.price FROM orders o
        JOIN accounts a ON o.account_id=a.id {w} ORDER BY o.id DESC LIMIT 10 OFFSET ?"""
    if st == "all": cur.execute(q.format(w=""), (pg*10,))
    else: cur.execute(q.format(w="WHERE o.status=?"), (st, pg*10))
    rows = cur.fetchall()
    if not rows: return await c.message.edit_text("Заказов нет.", reply_markup=a_orders_menu())
    await c.message.edit_text(f"🧾 <b>{st} ({tot})</b>", reply_markup=a_orders_kb(rows, st, pg, tot), parse_mode="HTML")

@dp.callback_query(F.data.startswith("a_oi:"))
async def a_oi(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    oid = int(c.data.split(":")[1]); o = get_order(oid)
    if not o: return await c.answer("Не найден", show_alert=True)
    a = get_acc(o[2])
    emo = {"pending":"⏳ Ожидает","receipt_sent":"🧾 Скрин на проверке","paid":"💰 Оплачен",
           "number_issued":"📱 Номер выдан","issued":"✅ Завершён","rejected":"❌ Отклонён","refunded":"↩️ Возврат"}
    txt = f"🧾 <b>Заказ №{oid}</b>\n\n👤 ID: <code>{o[1]}</code>\n🌍 {a[1]}\n📱 {a[2] or '🔒'}\n💰 {a[3]}₽\n📌 <b>{emo.get(o[3], o[3])}</b>"
    if o[9]: txt += f"\n📝 Заметка: {o[9]}"
    await c.message.edit_text(txt, reply_markup=a_order_info_kb(oid, o[3]), parse_mode="HTML")

@dp.callback_query(F.data.startswith("a_note:"))
async def a_note(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    oid = int(c.data.split(":")[1])
    await state.set_state(S.note); await state.update_data(oid=oid)
    await c.message.edit_text(f"📝 Введите заметку для заказа №{oid}:", reply_markup=a_back("a_orders"))

@dp.message(S.note)
async def a_note_save(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    data = await state.get_data(); order_note(data["oid"], m.text.strip()); await state.clear()
    await m.answer("✅ Заметка сохранена", reply_markup=admin_menu())

@dp.callback_query(F.data.startswith("a_refund:"))
async def a_refund(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    oid = int(c.data.split(":")[1]); o = get_order(oid)
    if not o: return await c.answer("Не найден", show_alert=True)
    mark_refunded(oid)
    await notify(o[1], f"↩️ <b>Возврат по заказу №{oid}</b>\n\nСредства возвращены.", parse_mode="HTML")
    await c.message.edit_text(f"↩️ Заказ №{oid} — возврат оформлен", reply_markup=a_back("a_orders"))

@dp.callback_query(F.data == "a_search")
async def a_search(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    await state.set_state(S.search)
    await c.message.edit_text("🔍 Введите номер заказа или ID пользователя:", reply_markup=a_back("a_orders"))

@dp.message(S.search)
async def a_search_do(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    q = m.text.strip(); await state.clear()
    if q.isdigit():
        o = get_order(int(q))
        if o:
            a = get_acc(o[2])
            return await m.answer(f"🧾 Заказ №{o[0]}\n👤 {o[1]}\n🌍 {a[1]}\n💰 {a[3]}₽\n📌 {o[3]}", reply_markup=a_order_info_kb(o[0], o[3]))
        cur.execute("SELECT o.id,o.status,a.country,a.price FROM orders o JOIN accounts a ON o.account_id=a.id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 10", (int(q),))
        rows = cur.fetchall()
        if rows: return await m.answer("📋 Заказы пользователя:", reply_markup=my_orders_kb(rows))
    await m.answer("❌ Не найдено", reply_markup=admin_menu())

# --- confirm / reject ---
@dp.callback_query(F.data.startswith("admin_confirm_pay:"))
async def a_confirm(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    oid = int(c.data.split(":")[1]); o = get_order(oid)
    if not o: return await c.answer("Не найден", show_alert=True)
    mark_paid(oid)
    await notify(o[1], f"✅ <b>Оплата подтверждена!</b>\n\nЗаказ №{oid}\n⏳ Ждите номер.", parse_mode="HTML")
    await c.message.edit_text(f"✅ Заказ №{oid} подтверждён.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Выдать номер", callback_data=f"a_give_phone:{oid}")]]))

@dp.callback_query(F.data.startswith("admin_reject_pay:"))
async def a_reject(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    oid = int(c.data.split(":")[1]); o = get_order(oid)
    if not o: return await c.answer("Не найден", show_alert=True)
    mark_rejected(oid)
    await notify(o[1], f"❌ <b>Оплата не подтверждена</b>\n\nЗаказ №{oid} отклонён.\nЕсли уверены — напишите в поддержку.", parse_mode="HTML")
    await c.message.edit_text(f"❌ Заказ №{oid} отклонён.", reply_markup=a_back())

# --- give phone ---
@dp.callback_query(F.data.startswith("a_give_phone:"))
async def a_gp(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    oid = int(c.data.split(":")[1])
    await state.set_state(S.give_phone); await state.update_data(oid=oid)
    await c.message.edit_text(f"📱 Номер для заказа №{oid}:", reply_markup=a_back("a_orders"))

@dp.message(S.give_phone)
async def a_gp_save(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    data = await state.get_data(); oid = data["oid"]; o = get_order(oid)
    ph = m.text.strip()
    if len(ph) < 5: return await m.answer("Короткий номер")
    a = get_acc(o[2]); save_phone(a[0], ph); mark_number(oid)
    await notify(o[1], f"📱 <b>Ваш номер готов! — {SHOP_NAME}</b>\n\n🌍 {a[1]}\n📞 <code>{ph}</code>\n\n🔑 Код придёт отдельным сообщением.", parse_mode="HTML")
    await state.clear()
    await m.answer("✅ Номер отправлен", reply_markup=admin_menu())

# --- give code ---
@dp.callback_query(F.data.startswith("a_give_code:"))
async def a_gc(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    oid = int(c.data.split(":")[1])
    await state.set_state(S.give_code); await state.update_data(oid=oid)
    await c.message.edit_text(f"🔑 Код для заказа №{oid}:", reply_markup=a_back("a_orders"))

@dp.message(S.give_code)
async def a_gc_save(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    data = await state.get_data(); oid = data["oid"]; o = get_order(oid)
    code = m.text.strip()
    if len(code) < 2: return await m.answer("Короткий код")
    a = get_acc(o[2]); mark_issued(oid)
    await notify(o[1], f"🔑 <b>Ваш код! — {SHOP_NAME}</b>\n\n🌍 {a[1]}\n📞 {a[2]}\n🔑 <code>{code}</code>\n\n📌 Сохраните.", parse_mode="HTML")
    await state.clear()
    await m.answer("✅ Код отправлен. Заказ завершён.", reply_markup=admin_menu())

# --- users ---
@dp.callback_query(F.data.startswith("a_users:"))
async def a_users(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    pg = int(c.data.split(":")[1])
    cur.execute("SELECT COUNT(*) FROM users"); tot = cur.fetchone()[0]
    cur.execute("SELECT user_id,username,blocked FROM users ORDER BY last_seen DESC LIMIT 10 OFFSET ?", (pg*10,))
    rows = cur.fetchall()
    if not rows: return await c.message.edit_text("Пусто.", reply_markup=admin_menu())
    await c.message.edit_text(f"👥 <b>Пользователи ({tot})</b>", reply_markup=a_users_kb(rows, pg, tot), parse_mode="HTML")

@dp.callback_query(F.data.startswith("a_user:"))
async def a_user(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    uid = int(c.data.split(":")[1])
    cur.execute("SELECT username, blocked, first_seen FROM users WHERE user_id=?", (uid,)); r = cur.fetchone()
    if not r: return await c.answer("Не найден", show_alert=True)
    cnt, spent = get_user_stats(uid)
    await c.message.edit_text(
        f"👤 <b>Пользователь</b>\n\n🆔 <code>{uid}</code>\n👤 @{r[0] or '—'}\n🚫 Блок: {'да' if r[1] else 'нет'}\n"
        f"🛒 Куплено: {cnt}\n💵 Потратил: {spent}₽",
        reply_markup=a_user_kb(uid, r[1]), parse_mode="HTML")

@dp.callback_query(F.data.startswith("a_block:"))
async def a_block(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    uid = int(c.data.split(":")[1])
    cur.execute("SELECT blocked FROM users WHERE user_id=?", (uid,)); b = cur.fetchone()[0]
    cur.execute("UPDATE users SET blocked=? WHERE user_id=?", (0 if b else 1, uid)); conn.commit()
    await c.answer("Обновлено")
    await a_user(c)

# --- broadcast ---
@dp.callback_query(F.data == "a_broadcast")
async def a_bcast(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    await state.set_state(S.broadcast)
    await c.message.edit_text("📢 Введите сообщение для рассылки (текст/фото):", reply_markup=a_back())

@dp.message(S.broadcast)
async def a_bcast_send(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.clear()
    cur.execute("SELECT user_id FROM users WHERE blocked=0"); users = [r[0] for r in cur.fetchall()]
    sent = 0; err = 0
    for uid in users:
        try:
            await m.copy_to(uid); sent += 1
            await asyncio.sleep(0.05)
        except: err += 1
    await m.answer(f"📢 Рассылка завершена\n\n✅ Отправлено: {sent}\n❌ Ошибок: {err}", reply_markup=admin_menu())

# --- settings ---
@dp.callback_query(F.data == "a_settings")
async def a_set(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    await c.message.edit_text(
        f"⚙️ <b>Настройки</b>\n\n"
        f"📞 Телефон: <code>{get_setting('pay_phone')}</code>\n"
        f"🏦 Банк: <b>{get_setting('pay_bank')}</b>\n"
        f"👤 Получатель: <b>{get_setting('pay_name')}</b>",
        reply_markup=a_settings_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "a_set_phone")
async def a_set_p(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    await state.set_state(S.set_pay_phone); await c.message.edit_text("📞 Новый телефон:", reply_markup=a_back("a_settings"))

@dp.message(S.set_pay_phone)
async def a_set_p_s(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    set_setting("pay_phone", m.text.strip()); await state.clear()
    await m.answer(f"✅ Телефон: <code>{m.text.strip()}</code>", reply_markup=admin_menu())

@dp.callback_query(F.data == "a_set_bank")
async def a_set_b(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    await state.set_state(S.set_pay_bank); await c.message.edit_text("🏦 Новый банк:", reply_markup=a_back("a_settings"))

@dp.message(S.set_pay_bank)
async def a_set_b_s(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    set_setting("pay_bank", m.text.strip()); await state.clear()
    await m.answer(f"✅ Банк: {m.text.strip()}", reply_markup=admin_menu())

@dp.callback_query(F.data == "a_set_name")
async def a_set_n(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    await state.set_state(S.set_pay_name); await c.message.edit_text("👤 Новый получатель:", reply_markup=a_back("a_settings"))

@dp.message(S.set_pay_name)
async def a_set_n_s(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    set_setting("pay_name", m.text.strip()); await state.clear()
    await m.answer(f"✅ Получатель: {m.text.strip()}", reply_markup=admin_menu())

@dp.callback_query(F.data == "a_tmpls")
async def a_tmpls(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    ts = list_tmpls()
    kb = [[InlineKeyboardButton(text=f"🗑 {t[1][:30]}", callback_data=f"a_del_tmpl:{t[0]}")] for t in ts]
    kb.append([InlineKeyboardButton(text="➕ Добавить", callback_data="a_add_tmpl")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="a_settings")])
    await c.message.edit_text("📝 <b>Шаблоны ответов</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

@dp.callback_query(F.data == "a_add_tmpl")
async def a_add_tmpl(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    await state.set_state(S.tmpl_add); await c.message.edit_text("Введите текст шаблона:", reply_markup=a_back("a_tmpls"))

@dp.message(S.tmpl_add)
async def a_tmpl_save(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    add_tmpl(m.text.strip()); await state.clear()
    await m.answer("✅ Шаблон добавлен", reply_markup=admin_menu())

@dp.callback_query(F.data.startswith("a_del_tmpl:"))
async def a_del_tmpl(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    del_tmpl(int(c.data.split(":")[1])); await c.answer("Удалён")
    await a_tmpls(c)

# --- admins ---
@dp.callback_query(F.data == "a_admins")
async def a_adms(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    await c.message.edit_text("👑 <b>Администраторы</b>\n\nНажмите, чтобы снять права:", reply_markup=a_admins_kb(list_admins()), parse_mode="HTML")

@dp.callback_query(F.data == "a_add_admin")
async def a_aa(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    await state.set_state(S.add_admin)
    await c.message.edit_text("👑 Введите <b>ID</b> или перешлите сообщение пользователя:", reply_markup=a_back("a_admins"))

@dp.message(S.add_admin)
async def a_aa_save(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    uid = None
    if m.text and m.text.strip().isdigit(): uid = int(m.text.strip())
    elif m.forward_from: uid = m.forward_from.id
    if not uid: return await m.answer("❌ Введите ID или перешлите сообщение")
    cur.execute("SELECT username FROM users WHERE user_id=?", (uid,)); r = cur.fetchone()
    add_admin(uid, r[0] if r else None, m.from_user.id)
    await state.clear()
    await m.answer(f"✅ @{r[0] if r else uid} теперь админ (ID {uid})", reply_markup=admin_menu())
    await notify(uid, f"👑 Вам выданы права администратора в {SHOP_NAME}!\n\nНапишите /admin для входа.", parse_mode="HTML")

@dp.callback_query(F.data.startswith("a_rm_admin:"))
async def a_rm(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    uid = int(c.data.split(":")[1])
    if uid == SUPER_ADMIN_ID: return await c.answer("Нельзя снять супер-админа", show_alert=True)
    rm_admin(uid); await c.answer("Снят")
    await a_adms(c)

@dp.callback_query(F.data.startswith("a_close_t:"))
async def a_close_t(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("⛔", show_alert=True)
    cur.execute("UPDATE tickets SET status='closed' WHERE id=?", (int(c.data.split(":")[1]),)); conn.commit()
    await c.message.edit_text("🔒 Тикет закрыт.")

# ============ REMINDER ============
async def reminder_loop():
    while True:
        try:
            now = int(time.time()); th = now - REMINDER_MIN*60
            cur.execute("SELECT id,user_id,account_id FROM orders WHERE status='paid' AND paid_at < ? AND reminded_number=0", (th,))
            for oid,uid,aid in cur.fetchall():
                a = get_acc(aid)
                if not a: continue
                try:
                    await bot.send_message(SUPER_ADMIN_ID, f"⏰ Заказ №{oid} оплачен, но номер не выдан.\n🌍 {a[1]}",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Выдать", callback_data=f"a_give_phone:{oid}")]]))
                    cur.execute("UPDATE orders SET reminded_number=1 WHERE id=?", (oid,)); conn.commit()
                except: pass
            cur.execute("SELECT id,user_id,account_id FROM orders WHERE status='number_issued' AND number_issued_at < ? AND reminded_code=0", (th,))
            for oid,uid,aid in cur.fetchall():
                a = get_acc(aid)
                if not a: continue
                try:
                    await bot.send_message(SUPER_ADMIN_ID, f"⏰ Заказ №{oid}: номер выдан, но код нет.\n📞 {a[2]}",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔑 Выдать код", callback_data=f"a_give_code:{oid}")]]))
                    cur.execute("UPDATE orders SET reminded_code=1 WHERE id=?", (oid,)); conn.commit()
                except: pass
        except Exception as e: print(f"reminder: {e}")
        await asyncio.sleep(60)

# ============ MAIN ============
async def main():
    print(f"{SHOP_EMOJI} {SHOP_NAME} запуск. DB={DB_PATH}")
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())