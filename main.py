import os
import re
import sqlite3
import logging
import asyncio
import time
from datetime import datetime
from typing import Optional, Dict

import uvicorn
from fastapi import FastAPI, BackgroundTasks, Request
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, 
    CommandHandler, 
    CallbackQueryHandler, 
    ContextTypes
)

# ==========================================================
#                      CẤU HÌNH HỆ THỐNG
# ==========================================================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIG = {
    "bot_token": "8560020347:AAECTuhAhuIvYz2pvDmwXS9mK4nEN-g-0EM",
    "admin_id": 7816353760,
    "admin_handle": "@nth_dev", 
    "bank_name": "MSB",
    "bank_bin": "970426",
    "bank_stk": "96886693002613",
    "bank_owner": "NGUYEN THANH HOP",
    "aml_note": "⚠️ <b>LƯU Ý:</b> Hệ thống nghiêm cấm hành vi rửa tiền. Mọi nguồn tiền bẩn, tiền vi phạm pháp luật nếu bị phát hiện sẽ bị phong tỏa vĩnh viễn và cung cấp thông tin cho cơ quan chức năng.",
    "spam_delay": 2.0
}

DB_FILE = "system_v16.sqlite3"
user_cooldowns: Dict[int, float] = {}

class Status:
    PENDING = "CHO_THANH_TOAN"
    HOLDING = "BOT_DANG_GIU_TIEN"
    BUYER_DONE = "NGUOI_MUA_XAC_NHAN"
    PAYOUT_WAIT = "CHO_GIAI_NGAN"
    COMPLETED = "THANH_CONG"
    CANCELLED = "DA_HUY"

def calculate_fee(amount: int) -> int:
    if amount < 100000: return 5000
    elif amount < 500000: return 10000
    elif amount < 1000000: return 15000
    elif amount <= 2000000: return 20000
    else: return 30000

def check_spam(user_id: int) -> bool:
    now = time.time()
    last = user_cooldowns.get(user_id, 0)
    if now - last < CONFIG["spam_delay"]: return True
    user_cooldowns[user_id] = now
    return False

# ==========================================================
#                      DATABASE
# ==========================================================
class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE, group_id INTEGER, group_name TEXT, group_link TEXT,
                buyer_id INTEGER, buyer_name TEXT, buyer_user TEXT,
                seller_name TEXT, amount INTEGER, fee INTEGER, total_pay INTEGER,
                product_name TEXT, seller_bank_info TEXT, status TEXT, 
                qr_msg_id INTEGER, status_msg_id INTEGER, created_at TEXT)''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT, action TEXT, detail TEXT, created_at TEXT)''')

    def create_trade(self, data):
        with self.conn:
            self.conn.execute("""INSERT INTO trades 
                (code, group_id, group_name, group_link, buyer_id, buyer_name, buyer_user, seller_name, 
                 amount, fee, total_pay, product_name, status, created_at) 
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data['code'], data['group_id'], data['group_name'], data['group_link'], data['buyer_id'], data['buyer_name'],
                 data['buyer_user'], data['seller_name'], data['amount'], data['fee'],
                 data['total_pay'], data['product_name'], Status.PENDING, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    def log_action(self, code, action, detail):
        with self.conn:
            self.conn.execute("INSERT INTO logs (code, action, detail, created_at) VALUES (?,?,?,?)",
                             (code, action, detail, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    def get_trade(self, code):
        return self.conn.execute("SELECT * FROM trades WHERE code = ?", (code,)).fetchone()

    def update_trade(self, code, **kwargs):
        set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [code]
        with self.conn:
            self.conn.execute(f"UPDATE trades SET {set_clause} WHERE code = ?", values)
    
    def get_stats(self):
        with self.conn:
            return self.conn.execute("""SELECT COUNT(*) as total_count, SUM(amount) as total_amount, 
                SUM(fee) as total_fee FROM trades WHERE status = ?""", (Status.COMPLETED,)).fetchone()

db = Database()
app = FastAPI()
tg_app = Application.builder().token(CONFIG["bot_token"]).build()

# Webhook SePay
@app.post("/webhook")
async def sepay_webhook(request: Request):
    try:
        data = await request.json()
        content = str(data.get("content", "")).upper()
        raw_val = data.get("amount_in") or data.get("amount") or 0
        amount_in = int(re.sub(r"\D", "", str(raw_val))) if raw_val else 0
        match = re.search(r"GD(\d+)", content)
        if match:
            code = f"GD{match.group(1)}"
            asyncio.create_task(process_paid_invoice(code, amount_in))
        return {"status": "success"}
    except: return {"status": "error"}

async def process_paid_invoice(code, amount_received):
    trade = db.get_trade(code)
    if not trade or trade['status'] != Status.PENDING: return
    if int(amount_received) >= int(trade['total_pay']):
        db.update_trade(code, status=Status.HOLDING)
        msg = f"""<b>✅ GIAO DỊCH {code} ĐÃ NHẬN ĐỦ TIỀN</b>
━━━━━━━━━━━━━━━━━━━━
📦 <b>Sản phẩm:</b> {trade['product_name']}
💰 <b>Số tiền:</b> {amount_received:,} VND
🛡 <b>Trạng thái:</b> BOT ĐANG GIỮ TIỀN AN TOÀN

🚀 <b>YÊU CẦU:</b> Người bán giao hàng. Xong xuôi người mua bấm nút xác nhận."""
        btn = [[InlineKeyboardButton("✅ TÔI ĐÃ NHẬN ĐỦ HÀNG", callback_data=f"done_{code}")]]
        sent = await tg_app.bot.send_message(chat_id=trade['group_id'], text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(btn))
        db.update_trade(code, status_msg_id=sent.message_id)

# ==========================================================
#                      COMMANDS
# ==========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if check_spam(update.effective_user.id): return
    bot_info = await context.bot.get_me()
    keyboard = [
        [InlineKeyboardButton("➕ Thêm Bot Vào Nhóm", url=f"https://t.me/{bot_info.username}?startgroup=true")],
        [InlineKeyboardButton("📖 Hướng Dẫn & Biểu Phí", callback_data="ui_help")],
        [InlineKeyboardButton("📊 Thống Kê Giao Dịch", callback_data="ui_stats"), InlineKeyboardButton("👨‍💻 Admin", url=f"https://t.me/{CONFIG['admin_handle'][1:]}")]
    ]
    txt = f"<b>🌟 HỆ THỐNG TRUNG GIAN TỰ ĐỘNG PRO MAX 🌟</b>\n━━━━━━━━━━━━━━━━━━━━\nChào mừng bạn đến với nền tảng Giao Dịch An Toàn.\n\n{CONFIG['aml_note']}"
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def cmd_taogdtg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private": return
    try:
        parts = [p.strip() for p in update.message.text.replace("/taogdtg", "").split("|")]
        amount = int(re.sub(r"\D", "", parts[0]))
        product, seller = parts[1], parts[2]
        code = f"GD{int(datetime.now().timestamp())}"
        fee = calculate_fee(amount)
        total = amount + fee
        db.create_trade({"code": code, "group_id": update.effective_chat.id, "group_name": update.effective_chat.title, "group_link": "", "buyer_id": update.effective_user.id, "buyer_name": update.effective_user.full_name, "buyer_user": f"@{update.effective_user.username}", "seller_name": seller, "amount": amount, "fee": fee, "total_pay": total, "product_name": product})
        
        qr = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={total}&addInfo={code}&accountName={CONFIG['bank_owner'].replace(' ', '%20')}"
        txt = f"<b>🤝 ĐƠN MỚI: {code}</b>\n━━━━━━━━━━━━━━━━━━━━\n📦 <b>SP:</b> {product}\n👤 <b>Bán:</b> {seller}\n💵 <b>Tổng:</b> <code>{total:,}</code> VND\n📝 <b>Nội dung:</b> <code>{code}</code>"
        kb = [[InlineKeyboardButton("🔄 QR", callback_data=f"getqr_{code}"), InlineKeyboardButton("❌ Hủy", callback_data=f"cancel_{code}")]]
        await update.message.reply_photo(photo=qr, caption=txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
    except:
        await update.message.reply_text("❌ Cú pháp: <code>/taogdtg Tiền | Sản phẩm | @Seller</code>", parse_mode=ParseMode.HTML)

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2: return
    code, info = context.args[0].upper(), " ".join(context.args[1:])
    trade = db.get_trade(code)
    if not trade or f"@{update.effective_user.username}".lower() != trade['seller_name'].lower(): return
    if trade['status'] == Status.BUYER_DONE:
        db.update_trade(code, status=Status.PAYOUT_WAIT, seller_bank_info=info)
        admin_txt = f"🏛 <b>YÊU CẦU RÚT: {code}</b>\n💰 <b>Tiền:</b> {trade['amount']:,}\n💳 <b>Bank:</b> {info}"
        await context.bot.send_message(CONFIG['admin_id'], admin_txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ GIẢI NGÂN XONG", callback_data=f"adminpayout_{code}")]]))
        await update.message.reply_text("✅ Đã gửi yêu cầu rút tiền cho Admin.")

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    trade = db.get_trade(context.args[0].upper())
    if trade:
        await update.message.reply_text(f"🔍 <b>Đơn {trade['code']}</b>\nTrạng thái: <code>{trade['status']}</code>", parse_mode=ParseMode.HTML)

async def cmd_huy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    code = context.args[0].upper()
    trade = db.get_trade(code)
    if trade and trade['status'] == Status.PENDING:
        db.update_trade(code, status=Status.CANCELLED)
        await update.message.reply_text(f"✅ Đã hủy đơn {code}")

# ==========================================================
#                      CALLBACKS
# ==========================================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    username = f"@{update.effective_user.username}"

    if data == "ui_help":
        await query.edit_message_text("📖 <b>HƯỚNG DẪN:</b>\n1. Tạo đơn: /taogdtg\n2. CK cho Bot\n3. Bán giao hàng\n4. Mua xác nhận\n5. Bán /bank rút tiền.", parse_mode=ParseMode.HTML)
    elif data == "ui_stats":
        s = db.get_stats()
        await query.edit_message_text(f"📊 <b>THỐNG KÊ:</b>\nThành công: {s[0]} đơn\nPhí thu: {s[2]:,} VND", parse_mode=ParseMode.HTML)
    elif data.startswith("getqr_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade:
            qr = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={trade['total_pay']}&addInfo={code}"
            await query.message.reply_photo(photo=qr, caption=f"🔄 QR Đơn {code}")
    elif data.startswith("cancel_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade and trade['status'] == Status.PENDING:
            db.update_trade(code, status=Status.CANCELLED)
            await query.edit_message_caption(caption="❌ <b>ĐƠN ĐÃ HỦY</b>", parse_mode=ParseMode.HTML)
    elif data.startswith("done_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade and user_id == trade['buyer_id']:
            db.update_trade(code, status=Status.BUYER_DONE)
            await query.edit_message_text(f"<b>📦 ĐƠN {code} HOÀN TẤT</b>\n\nNgười bán dùng <code>/bank</code> để rút tiền.", parse_mode=ParseMode.HTML)
    elif data.startswith("adminpayout_"):
        if user_id == CONFIG['admin_id']:
            code = data.split("_")[1]
            trade = db.get_trade(code)
            db.update_trade(code, status=Status.COMPLETED)
            await query.edit_message_text(f"✅ Đã xác nhận giải ngân {code}")
            await context.bot.send_message(trade['group_id'], f"🎉 <b>ĐƠN {code} ĐÃ GIẢI NGÂN HOÀN TẤT!</b>", parse_mode=ParseMode.HTML)

# ==========================================================
#                      RUNNER
# ==========================================================
async def main_runner():
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("taogdtg", cmd_taogdtg))
    tg_app.add_handler(CommandHandler("bank", cmd_bank))
    tg_app.add_handler(CommandHandler("check", cmd_check))
    tg_app.add_handler(CommandHandler("huy", cmd_huy))
    tg_app.add_handler(CallbackQueryHandler(callback_handler))
    
    await tg_app.initialize()
    await tg_app.start()
    asyncio.create_task(tg_app.updater.start_polling())
    
    port = int(os.environ.get("PORT", 10000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main_runner())
                              
