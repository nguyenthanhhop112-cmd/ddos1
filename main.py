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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import (
    Application, 
    CommandHandler, 
    CallbackQueryHandler, 
    ContextTypes,
    MessageHandler,
    filters
)

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
}

DB_FILE = "system_pro_v7.sqlite3"

# TRẠNG THÁI GIAO DỊCH
ST_WAIT_PAY = "CHO_TT"         
ST_PAID_HOLDING = "BOT_GIU"       
ST_BUYER_DONE = "BUYER_XN"   
ST_WAIT_PAYOUT = "CHO_PAYOUT"    
ST_COMPLETED = "SUCCESS"              
ST_CANCELLED = "CANCEL"

# ==========================================================
#                      DATABASE ARCHITECTURE
# ==========================================================
class Database:
    def __init__(self):
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE, group_id INTEGER, group_name TEXT,
                buyer_id INTEGER, buyer_name TEXT, buyer_user TEXT,
                seller_id INTEGER, seller_name TEXT, 
                amount INTEGER, fee INTEGER, total_pay INTEGER,
                product_name TEXT, seller_bank TEXT, status TEXT, 
                qr_msg_id INTEGER, status_msg_id INTEGER, created_at TEXT)''')
            
            conn.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT,
                total_trades INTEGER DEFAULT 0, total_vol INTEGER DEFAULT 0)''')
            conn.commit()

    def create_trade(self, data):
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("""INSERT INTO trades 
                (code, group_id, group_name, buyer_id, buyer_name, buyer_user, seller_name, 
                 amount, fee, total_pay, product_name, status, created_at) 
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data['code'], data['group_id'], data['group_name'], data['buyer_id'], data['buyer_name'],
                 data['buyer_user'], data['seller_name'], data['amount'], data['fee'],
                 data['total_pay'], data['product_name'], ST_WAIT_PAY, datetime.now().isoformat()))

    def update_status(self, code, status, bank=None, qr_id=None, st_id=None):
        with sqlite3.connect(DB_FILE) as conn:
            if bank: conn.execute("UPDATE trades SET status = ?, seller_bank = ? WHERE code = ?", (status, bank, code))
            else: conn.execute("UPDATE trades SET status = ? WHERE code = ?", (status, code))
            if qr_id: conn.execute("UPDATE trades SET qr_msg_id = ? WHERE code = ?", (qr_id, code))
            if st_id: conn.execute("UPDATE trades SET status_msg_id = ? WHERE code = ?", (st_id, code))

    def get_trade(self, code):
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM trades WHERE code = ?", (code,)).fetchone()

    def get_user(self, user_id):
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

    def update_user(self, user_id, username, amount):
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?,?)", (user_id, username))
            conn.execute("UPDATE users SET total_trades = total_trades + 1, total_vol = total_vol + ? WHERE user_id = ?", (amount, user_id))

db = Database()
app = FastAPI()
tg_app = Application.builder().token(CONFIG["bot_token"]).build()

# ==========================================================
#                      WEBHOOK FIX (LỖI 422)
# ==========================================================
class SePayData(BaseModel):
    id: Optional[int] = None
    amount_in: int = 0
    content: str
    reference_number: Optional[str] = None
    account_number: Optional[str] = None

@app.post("/webhook")
async def sepay_webhook(data: SePayData, background_tasks: BackgroundTasks):
    match = re.search(r"GD\d+", data.content.upper())
    if match:
        background_tasks.add_task(on_paid_success, match.group(), data.amount_in)
    return {"status": "success"}

async def on_paid_success(code, amount):
    trade = db.get_trade(code)
    if not trade or trade['status'] != ST_WAIT_PAY: return
    if amount < trade['total_pay']: return

    db.update_status(code, ST_PAID_HOLDING)
    try: await tg_app.bot.unpin_chat_message(chat_id=trade['group_id'], message_id=trade['qr_msg_id'])
    except: pass

    msg = (
        f"✅ **ĐÃ NHẬN TIỀN THÀNH CÔNG**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 Mã đơn: `{code}`\n"
        f"💰 Số tiền: `{amount:,}đ`\n"
        f"🛡 Trạng thái: **BOT ĐANG GIỮ TIỀN**\n\n"
        f"🚀 Mời người bán **{trade['seller_name']}** giao hàng.\n"
        f"⚠️ Người mua kiểm tra xong hãy gõ: `/done {code}`"
    )
    sent = await tg_app.bot.send_message(chat_id=trade['group_id'], text=msg, parse_mode=ParseMode.MARKDOWN)
    db.update_status(code, ST_PAID_HOLDING, st_id=sent.message_id)
    try: await tg_app.bot.pin_chat_message(chat_id=trade['group_id'], message_id=sent.message_id)
    except: pass

# ==========================================================
#                      GIAO DIỆN & CHỨC NĂNG
# ==========================================================
async def start(update: Update, context):
    bot_info = await context.bot.get_me()
    add_link = f"https://t.me/{bot_info.username}?startgroup=true"
    
    keyboard = [
        [InlineKeyboardButton("➕ Thêm Bot vào nhóm", url=add_link)],
        [InlineKeyboardButton("📚 Hướng dẫn chi tiết", callback_data="help_guide")],
        [InlineKeyboardButton("👨‍💻 Liên hệ Admin", url=f"https://t.me/{CONFIG['admin_handle'][1:]}")]
    ]
    
    txt = (
        "💎 **HỆ THỐNG TRUNG GIAN AUTO V7.1**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Chào mừng bạn đến với nền tảng trung gian tự động hóa. "
        "Giúp các giao dịch mua bán online trở nên an toàn 100%.\n\n"
        "🔸 **Tự động nhận diện ngân hàng 24/7**\n"
        "🔸 **Minh bạch lịch sử & uy tín**\n"
        "🔸 **Xử lý giải ngân siêu tốc**"
    )
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def help_callback(update: Update, context):
    query = update.callback_query
    txt = (
        "📖 **HƯỚNG DẪN SỬ DỤNG CHI TIẾT**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ **Tạo đơn:** Tại nhóm, gõ:\n`/taogdtg | giá | tên SP | @nguoiban`\n"
        "2️⃣ **Thanh toán:** Người mua quét mã QR Bot gửi. Bot sẽ báo 'Đã giữ tiền' ngay khi nhận được.\n"
        "3️⃣ **Giao hàng:** Người bán thực hiện giao hàng.\n"
        "4️⃣ **Xác nhận:** Người mua kiểm tra xong, gõ `/done [mã_đơn]`.\n"
        "5️⃣ **Nhận tiền:** Người bán gửi STK bằng lệnh `/bank [mã_đơn] [STK]`. Admin sẽ giải ngân ngay."
    )
    await query.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="back_start")]]))

async def create_trade(update: Update, context):
    if update.effective_chat.type == "private": return
    try:
        args = [i.strip() for i in update.message.text.split("|")]
        amount = int(re.sub(r"\D", "", args[1]))
        code = f"GD{int(datetime.now().timestamp())}"
        fee = 5000 if amount < 100000 else 10000 # Phí mẫu
        total = amount + fee

        db.create_trade({
            "code": code, "group_id": update.effective_chat.id, "group_name": update.effective_chat.title,
            "buyer_id": update.effective_user.id, "buyer_name": update.effective_user.full_name,
            "buyer_user": f"@{update.effective_user.username}", "seller_name": args[3],
            "amount": amount, "fee": fee, "total_pay": total, "product_name": args[2]
        })

        qr = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={total}&addInfo={code}"
        cap = (
            f"🤝 **GIAO DỊCH MỚI: {code}**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📦 **SP:** {args[2]}\n"
            f"💰 **Giá:** {amount:,}đ | **Phí:** {fee:,}đ\n"
            f"💳 **Tổng thanh toán:** `{total:,}đ`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ **Nội dung:** `{code}`"
        )
        msg = await update.message.reply_photo(photo=qr, caption=cap, parse_mode=ParseMode.MARKDOWN)
        db.update_status(code, ST_WAIT_PAY, qr_id=msg.message_id)
        try: await update.message.pin() 
        except: pass
    except:
        await update.message.reply_text("❌ Sai cú pháp! `/taogdtg | giá | sản phẩm | @nguoiban`")

async def profile(update: Update, context):
    user = db.get_user(update.effective_user.id)
    if not user:
        return await update.message.reply_text("✨ Bạn chưa có lịch sử giao dịch trên hệ thống.")
    
    txt = (
        f"👤 **HỒ SƠ UY TÍN: {update.effective_user.full_name}**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ Số đơn hoàn tất: `{user['total_trades']}`\n"
        f"💰 Tổng hạn mức: `{user['total_vol']:,}đ`\n"
        f"🌟 Đánh giá: ⭐⭐⭐⭐⭐"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

# ==========================================================
#                      KHỞI CHẠY (FIX RUNTIME)
# ==========================================================
async def runner():
    # Handlers
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("taogdtg", create_trade))
    tg_app.add_handler(CommandHandler("profile", profile))
    tg_app.add_handler(CallbackQueryHandler(help_callback, pattern="help_guide"))
    # Thêm các command khác tương tự...

    await tg_app.initialize()
    await tg_app.start()
    asyncio.create_task(tg_app.updater.start_polling())

    port = int(os.environ.get("PORT", 10000))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio"))
    await server.serve()

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(runner())
    except (KeyboardInterrupt, SystemExit):
        pass
                 
