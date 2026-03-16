import os
import re
import sqlite3
import logging
import asyncio
from datetime import datetime
from typing import Optional

import uvicorn
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, 
    CommandHandler, 
    CallbackQueryHandler, 
    ContextTypes
)

# Cấu hình Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================================
#                      CẤU HÌNH HỆ THỐNG
# ==========================================================
CONFIG = {
    "bot_token": "8560020347:AAECTuhAhuIvYz2pvDmwXS9mK4nEN-g-0EM",
    "admin_id": 7816353760,
    "admin_handle": "@nth_dev", 
    "bank_name": "MSB",
    "bank_bin": "970426",
    "bank_stk": "96886693002613",
    "bank_owner": "NGUYEN THANH HOP",
    "fee_min": 5000,
    "fee_percent": 0.01  # 1% phí
}

DB_FILE = "database_v9_ultimate.sqlite3"

class Status:
    PENDING = "⏳ CHỜ THANH TOÁN"
    HOLDING = "🛡️ BOT ĐANG GIỮ TIỀN"
    BUYER_DONE = "📦 BUYER ĐÃ NHẬN HÀNG"
    PAYOUT_WAIT = "💸 ĐANG CHỜ GIẢI NGÂN"
    COMPLETED = "✅ GIAO DỊCH HOÀN TẤT"
    CANCELLED = "❌ ĐÃ HỦY"

# ==========================================================
#                      DATABASE ENGINE
# ==========================================================
class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()

    def create_tables(self):
        with self.conn:
            self.conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE, group_id INTEGER, group_name TEXT,
                buyer_id INTEGER, buyer_name TEXT, buyer_user TEXT,
                seller_id INTEGER, seller_name TEXT, 
                amount INTEGER, fee INTEGER, total_pay INTEGER,
                product_name TEXT, seller_bank_info TEXT, status TEXT, 
                qr_msg_id INTEGER, status_msg_id INTEGER, created_at TEXT)''')
            
            self.conn.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
                completed_trades INTEGER DEFAULT 0, total_volume INTEGER DEFAULT 0)''')

    def save_trade(self, data):
        with self.conn:
            self.conn.execute("""INSERT INTO trades 
                (code, group_id, group_name, buyer_id, buyer_name, buyer_user, seller_name, 
                 amount, fee, total_pay, product_name, status, created_at) 
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data['code'], data['group_id'], data['group_name'], data['buyer_id'], data['buyer_name'],
                 data['buyer_user'], data['seller_name'], data['amount'], data['fee'],
                 data['total_pay'], data['product_name'], Status.PENDING, datetime.now().isoformat()))

    def get_trade(self, code):
        return self.conn.execute("SELECT * FROM trades WHERE code = ?", (code,)).fetchone()

    def update_trade(self, code, **kwargs):
        keys = [f"{k} = ?" for k in kwargs.keys()]
        query = f"UPDATE trades SET {', '.join(keys)} WHERE code = ?"
        with self.conn: self.conn.execute(query, list(kwargs.values()) + [code])

    def update_user_stats(self, user_id, name, username, amount):
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?,?,?)", (user_id, username, name))
            self.conn.execute("UPDATE users SET completed_trades = completed_trades + 1, total_volume = total_volume + ? WHERE user_id = ?", (amount, user_id))

db = Database()
tg_app = Application.builder().token(CONFIG["bot_token"]).build()
app = FastAPI()

# ==========================================================
#                      HÀM XỬ LÝ CHÍNH
# ==========================================================

async def notify_payout_to_admin(trade, bank_info):
    """Gửi yêu cầu giải ngân cho Admin"""
    btn = [[InlineKeyboardButton("✅ XÁC NHẬN ĐÃ PAYOUT", callback_data=f"admin_payout_{trade['code']}")]]
    txt = (
        f"🏛️ **YÊU CẦU GIẢI NGÂN MỚI**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Mã đơn: `{trade['code']}`\n"
        f"💰 Số tiền: `{trade['amount']:,}đ`\n"
        f"💳 **STK ĐÍCH:** `{bank_info}`\n"
        f"📍 Nhóm: {trade['group_name']}\n"
        f"👤 Seller: {trade['seller_name']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Vui lòng bank xong mới bấm xác nhận!"
    )
    await tg_app.bot.send_message(chat_id=CONFIG['admin_id'], text=txt, reply_markup=InlineKeyboardMarkup(btn), parse_mode=ParseMode.MARKDOWN)

# ==========================================================
#                      WEBHOOK XỬ LÝ TIỀN VÀO
# ==========================================================
class SePayWebhook(BaseModel):
    id: int
    amount_in: int
    content: str
    code: Optional[str] = None

@app.post("/webhook")
async def sepay_handler(data: SePayWebhook, background_tasks: BackgroundTasks):
    logger.info(f"Dữ liệu SePay: {data.content} | {data.amount_in}")
    match = re.search(r"GD\d+", data.content.upper())
    if match:
        code = match.group()
        background_tasks.add_task(handle_payment_logic, code, data.amount_in)
    return {"status": "success"}

async def handle_payment_logic(code, amount):
    trade = db.get_trade(code)
    if not trade or trade['status'] != Status.PENDING: return
    if amount < trade['total_pay']: return

    db.update_trade(code, status=Status.HOLDING)
    
    # Gỡ ghim QR
    try: await tg_app.bot.unpin_chat_message(chat_id=trade['group_id'], message_id=trade['qr_msg_id'])
    except: pass

    # Thông báo nổ tiền vào đúng nhóm
    btn = [[InlineKeyboardButton("✅ BUYER XÁC NHẬN ĐÃ NHẬN HÀNG", callback_data=f"user_done_{code}")]]
    txt = (
        f"✨ **THÔNG BÁO: ĐÃ NHẬN TIỀN**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 Mã đơn: `{code}`\n"
        f"💰 Số dư: `+{amount:,}đ`\n"
        f"🛡️ Trạng thái: **HỆ THỐNG ĐANG GIỮ TIỀN**\n\n"
        f"👤 Người mua: {trade['buyer_name']}\n"
        f"👤 Người bán: **{trade['seller_name']}**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 Mời người bán giao hàng. Sau khi nhận đủ, Buyer hãy bấm nút xác nhận bên dưới."
    )
    sent = await tg_app.bot.send_message(chat_id=trade['group_id'], text=txt, reply_markup=InlineKeyboardMarkup(btn), parse_mode=ParseMode.MARKDOWN)
    db.update_trade(code, status_msg_id=sent.message_id)
    try: await tg_app.bot.pin_chat_message(chat_id=trade['group_id'], message_id=sent.message_id)
    except: pass

# ==========================================================
#                      TELEGRAM COMMANDS
# ==========================================================

async def cmd_start(update: Update, context):
    me = await context.bot.get_me()
    btn = [
        [InlineKeyboardButton("➕ Thêm Bot vào Nhóm", url=f"https://t.me/{me.username}?startgroup=true")],
        [InlineKeyboardButton("📜 Hướng dẫn", callback_data="guide"), InlineKeyboardButton("📊 Uy tín", callback_data="profile")],
        [InlineKeyboardButton("👨‍💻 Admin Support", url=f"https://t.me/{CONFIG['admin_handle'][1:]}")]
    ]
    txt = (
        f"👋 **Chào mừng bạn đến với {me.first_name}!**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Hệ thống trung gian tự động hóa 4.0\n"
        f"🔹 **An toàn:** Bot giữ tiền, chỉ giải ngân khi khách xong.\n"
        f"🔹 **Tốc độ:** Nhận diện Bank nội địa 24/7 chỉ 3s.\n"
        f"🔹 **Uy tín:** Minh bạch mọi giao dịch."
    )
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(btn), parse_mode=ParseMode.MARKDOWN)

async def cmd_taogdtg(update: Update, context):
    if update.effective_chat.type == "private":
        return await update.message.reply_text("❌ Lệnh này phải được thực hiện trong **Nhóm giao dịch**!")
    
    try:
        parts = [p.strip() for p in update.message.text.split("|")]
        amount = int(re.sub(r"\D", "", parts[1]))
        product = parts[2]
        seller = parts[3]
        
        code = f"GD{int(datetime.now().timestamp())}"
        fee = max(CONFIG['fee_min'], int(amount * CONFIG['fee_percent']))
        total = amount + fee

        db.save_trade({
            "code": code, "group_id": update.effective_chat.id, "group_name": update.effective_chat.title,
            "buyer_id": update.effective_user.id, "buyer_name": update.effective_user.full_name,
            "buyer_user": f"@{update.effective_user.username or 'NoUser'}", "seller_name": seller,
            "amount": amount, "fee": fee, "total_pay": total, "product_name": product
        })

        qr_url = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={total}&addInfo={code}&accountName={CONFIG['bank_owner']}"
        
        txt = (
            f"🤝 **GIAO DỊCH TRUNG GIAN MỚI**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Mã đơn: `{code}`\n"
            f"📦 Sản phẩm: **{product}**\n"
            f"👤 Người bán: {seller}\n"
            f"👤 Người mua: {update.effective_user.full_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Tiền hàng: `{amount:,}đ`\n"
            f"⚙️ Phí dịch vụ: `{fee:,}đ`\n"
            f"💳 **TỔNG THANH TOÁN:** `{total:,}đ`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ **NỘI DUNG CHUYỂN KHOẢN:** `{code}`"
        )
        msg = await update.message.reply_photo(photo=qr_url, caption=txt, parse_mode=ParseMode.MARKDOWN)
        db.update_trade(code, qr_msg_id=msg.message_id)
        try: await msg.pin()
        except: pass
        
    except:
        await update.message.reply_text("❌ **LỖI CÚ PHÁP!**\nSử dụng: `/taogdtg | giá | sản phẩm | @nguoiban`")

async def callback_handler(update: Update, context):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    # Buyer xác nhận nhận hàng
    if data.startswith("user_done_"):
        code = data.split("_")[2]
        trade = db.get_trade(code)
        if trade and user_id == trade['buyer_id'] and trade['status'] == Status.HOLDING:
            db.update_trade(code, status=Status.BUYER_DONE)
            try: await context.bot.unpin_chat_message(chat_id=trade['group_id'], message_id=trade['status_msg_id'])
            except: pass
            
            txt = (
                f"📦 **XÁC NHẬN HOÀN TẤT**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ Người mua đã xác nhận nhận hàng cho đơn `{code}`.\n"
                f"🔔 Mời người bán **{trade['seller_name']}** gửi thông tin nhận tiền.\n\n"
                f"👉 **Cú pháp:** `/bank {code} [STK + Ngân hàng]`"
            )
            await query.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN)

    # Admin xác nhận giải ngân
    elif data.startswith("admin_payout_"):
        if user_id != CONFIG['admin_id']: return
        code = data.split("_")[2]
        trade = db.get_trade(code)
        if trade:
            db.update_trade(code, status=Status.COMPLETED)
            db.update_user_stats(trade['buyer_id'], trade['buyer_name'], trade['buyer_user'], trade['amount'])
            await query.edit_message_text(f"✅ Đơn `{code}` đã giải ngân thành công!")
            await tg_app.bot.send_message(chat_id=trade['group_id'], text=f"🎉 **GIAO DỊCH {code} THÀNH CÔNG**\nAdmin đã chuyển tiền cho người bán. Cảm ơn các bạn đã tin dùng!")

async def cmd_bank(update: Update, context):
    if len(context.args) < 2: return
    code = context.args[0].upper()
    bank_info = " ".join(context.args[1:])
    trade = db.get_trade(code)
    
    if trade and trade['status'] == Status.BUYER_DONE:
        db.update_trade(code, status=Status.PAYOUT_WAIT, seller_bank_info=bank_info)
        await notify_payout_to_admin(trade, bank_info)
        await update.message.reply_text("✅ **ĐÃ GỬI STK!**\nAdmin sẽ kiểm tra và giải ngân tiền cho bạn ngay bây giờ.")

# ==========================================================
#                      RUNNER
# ==========================================================
async def run_system():
    # Handlers
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("taogdtg", cmd_taogdtg))
    tg_app.add_handler(CommandHandler("bank", cmd_bank))
    tg_app.add_handler(CallbackQueryHandler(callback_handler))

    # Start Telegram
    await tg_app.initialize()
    await tg_app.start()
    asyncio.create_task(tg_app.updater.start_polling())

    # Start FastAPI
    port = int(os.environ.get("PORT", 10000))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio"))
    await server.serve()

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_system())
        
