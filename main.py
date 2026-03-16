import os
import re
import sqlite3
import logging
import asyncio
import threading
import urllib.parse
from datetime import datetime
from typing import Optional, Dict, Any

import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    Bot
)
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
    "admin_id": 7816353760,  # ID Telegram của Admin để nhận báo cáo
    "admin_handle": "@nth_dev", 
    "bank_name": "MSB",
    "bank_bin": "970426",
    "bank_stk": "96886693002613",
    "bank_owner": "NGUYEN THANH HOP",
}

DB_FILE = "gdtg_final.sqlite3"

# Các trạng thái của đơn hàng
STATUS_WAIT_PAY = "CHO_THANH_TOAN"
STATUS_PAID = "DA_THANH_TOAN_BOT_GIU"
STATUS_DONE_WAIT_BANK = "CHO_STK_NGUOI_BAN"
STATUS_WAIT_PAYOUT = "CHO_ADMIN_CHUYEN_TIEN"
STATUS_COMPLETED = "HOAN_TAT"

# ==========================================================
#                      LOGIC TÍNH PHÍ
# ==========================================================
def calculate_fee(amount: int) -> int:
    if amount < 100000:
        return 5000
    elif 100000 <= amount < 500000:
        return 10000
    elif 500000 <= amount < 1000000:
        return 15000
    elif 1000000 <= amount <= 2000000:
        return 20000
    else:
        fee = int(amount * 0.01)
        return min(fee, 30000)

# ==========================================================
#                      DATABASE MANAGER
# ==========================================================
class Database:
    def __init__(self):
        self.init_db()

    def init_db(self):
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                group_id INTEGER,
                group_name TEXT,
                buyer_id INTEGER,
                buyer_name TEXT,
                seller_id INTEGER,
                seller_name TEXT,
                amount INTEGER,
                fee INTEGER,
                total_pay INTEGER,
                product_name TEXT,
                seller_bank TEXT,
                status TEXT,
                created_at TEXT
            )''')
            conn.commit()

    def create_trade(self, data: dict):
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("""INSERT INTO trades 
                (code, group_id, group_name, buyer_id, buyer_name, seller_id, seller_name, 
                 amount, fee, total_pay, product_name, status, created_at) 
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data['code'], data['group_id'], data['group_name'], data['buyer_id'], data['buyer_name'],
                 data['seller_id'], data['seller_name'], data['amount'], data['fee'],
                 data['total_pay'], data['product_name'], STATUS_WAIT_PAY, datetime.now().isoformat()))
            conn.commit()

    def get_trade(self, code):
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM trades WHERE code = ?", (code,)).fetchone()

    def update_status(self, code, status, seller_bank=None):
        with sqlite3.connect(DB_FILE) as conn:
            if seller_bank:
                conn.execute("UPDATE trades SET status = ?, seller_bank = ? WHERE code = ?", (status, seller_bank, code))
            else:
                conn.execute("UPDATE trades SET status = ? WHERE code = ?", (status, code))
            conn.commit()

db = Database()

# ==========================================================
#                      FASTAPI & WEBHOOK (SEPAY)
# ==========================================================
app = FastAPI()
telegram_app = Application.builder().token(CONFIG["bot_token"]).build()

class SePayData(BaseModel):
    content: str
    amountIn: int
    transactionDate: str

@app.get("/")
def home():
    return {"status": "Bot GDTG is running", "webhook_path": "/webhook"}

@app.post("/webhook")
async def sepay_webhook(data: SePayData, background_tasks: BackgroundTasks):
    # Tìm mã GD trong nội dung chuyển khoản của SePay
    match = re.search(r"GD\d+", data.content.upper())
    if match:
        trade_code = match.group()
        background_tasks.add_task(handle_payment_success, trade_code, data.amountIn)
    return {"status": "received"}

async def handle_payment_success(code, amount):
    trade = db.get_trade(code)
    if trade and trade['status'] == STATUS_WAIT_PAY:
        # Kiểm tra nếu khách chuyển đủ tiền (hoặc nhiều hơn)
        if amount >= trade['total_pay']:
            db.update_status(code, STATUS_PAID)
            
            # Thông báo nhóm
            msg = (
                f"✅ **XÁC NHẬN: ĐÃ NHẬN TIỀN TỰ ĐỘNG**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🆔 Mã đơn: `{code}`\n"
                f"💰 Số tiền vào: {amount:,}đ\n"
                f"👤 Người mua: {trade['buyer_name']}\n"
                f"🛡 Trạng thái: **BOT ĐÃ GIỮ TIỀN AN TOÀN**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🚀 Mời người bán **{trade['seller_name']}** giao hàng.\n"
                f"💡 Khi nhận xong, người mua nhấn: `/done {code}`"
            )
            await telegram_app.bot.send_message(chat_id=trade['group_id'], text=msg, parse_mode=ParseMode.MARKDOWN)

            # Báo cho Admin biết có tiền vào
            admin_notif = (
                f"💰 **THÔNG BÁO TIỀN VÀO (WEBHOOK)**\n"
                f"🆔 Đơn: `{code}`\n"
                f"💵 Số tiền thực nhận: {amount:,}đ\n"
                f"📍 Nhóm: {trade['group_name']}"
            )
            await telegram_app.bot.send_message(chat_id=CONFIG['admin_id'], text=admin_notif)

# ==========================================================
#                      BOT COMMANDS
# ==========================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💎 **BOT TRUNG GIAN AUTO V7**\n"
        "*(Tích hợp Webhook SePay - Duyệt tay an toàn)*\n\n"
        "📍 **Lệnh trong nhóm:**\n"
        "👉 `/taogdtg | giá | sản phẩm | @người_bán`\n"
        "👉 `/done [mã_đơn]` - Người mua xác nhận nhận hàng.\n"
        "👉 `/bank [mã_đơn] [STK]` - Người bán gửi thông tin nhận tiền."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def create_trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return await update.message.reply_text("⚠️ Vui lòng thêm Bot vào Nhóm để sử dụng!")

    try:
        parts = [p.strip() for p in update.message.text.split("|")]
        amount = int(re.sub(r"\D", "", parts[1]))
        product = parts[2]
        seller_mention = parts[3]
        
        buyer = update.effective_user
        code = f"GD{int(datetime.now().timestamp())}"
        fee = calculate_fee(amount)
        total = amount + fee

        db.create_trade({
            "code": code, "group_id": update.effective_chat.id, "group_name": update.effective_chat.title,
            "buyer_id": buyer.id, "buyer_name": buyer.full_name, "seller_id": 0, "seller_name": seller_mention,
            "amount": amount, "fee": fee, "total_pay": total, "product_name": product
        })

        qr_url = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={total}&addInfo={code}&accountName={CONFIG['bank_owner']}"
        caption = (
            f"🤝 **GIAO DỊCH TRUNG GIAN: {code}**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📦 **Sản phẩm:** {product}\n"
            f"👤 **Người mua:** {buyer.full_name}\n"
            f"👤 **Người bán:** {seller_mention}\n"
            f"💰 **Giá:** {amount:,}đ | **Phí:** {fee:,}đ\n"
            f"💳 **Tổng thanh toán:** `{total:,}`đ\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📝 **Nội dung:** `{code}`"
        )
        await update.message.reply_photo(photo=qr_url, caption=caption, parse_mode=ParseMode.MARKDOWN)

        # Báo Admin có đơn mới
        await telegram_app.bot.send_message(chat_id=CONFIG['admin_id'], text=f"🆕 **ĐƠN MỚI:** `{code}` tại nhóm `{update.effective_chat.title}`\nGiá: {amount:,}đ")
    except:
        await update.message.reply_text("❌ Lỗi cú pháp! Ví dụ: `/taogdtg | 100000 | Tên SP | @nguoiban`")

async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("⚠️ `/done [mã_đơn]`")
    
    code = context.args[0].upper()
    trade = db.get_trade(code)
    
    if not trade or update.effective_user.id != trade['buyer_id'] or trade['status'] != STATUS_PAID:
        return await update.message.reply_text("❌ Đơn không tồn tại hoặc bạn không phải người mua.")

    db.update_status(code, STATUS_DONE_WAIT_BANK)
    await update.message.reply_text(f"✅ Người mua đã xác nhận! Mời {trade['seller_name']} gửi STK:\n👉 `/bank {code} [Thông tin STK]`")
    await telegram_app.bot.send_message(chat_id=CONFIG['admin_id'], text=f"🔔 Đơn `{code}`: Buyer đã bấm /done. Chờ STK của Seller...")

async def bank_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2: return await update.message.reply_text("⚠️ `/bank [mã_đơn] [STK]`")

    code = context.args[0].upper()
    bank_info = " ".join(context.args[1:])
    trade = db.get_trade(code)

    if not trade or trade['status'] != STATUS_DONE_WAIT_BANK:
        return await update.message.reply_text("❌ Đơn hàng không ở trạng thái chờ gửi STK.")

    db.update_status(code, STATUS_WAIT_PAYOUT, seller_bank=bank_info)

    # Gửi yêu cầu giải ngân cho Admin
    admin_msg = (
        f"🚨 **YÊU CẦU GIẢI NGÂN: {code}**\n"
        f"📍 Nhóm: {trade['group_name']}\n"
        f"👤 Seller: {trade['seller_name']}\n"
        f"💳 **STK:** `{bank_info}`\n"
        f"💵 **TIỀN TRẢ:** **{trade['amount']:,}đ**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Bấm nút dưới đây sau khi bạn đã chuyển khoản xong."
    )
    keyboard = [[InlineKeyboardButton("✅ XÁC NHẬN ĐÃ CHUYỂN TIỀN", callback_data=f"pay_{code}")]]
    await telegram_app.bot.send_message(chat_id=CONFIG['admin_id'], text=admin_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    await update.message.reply_text("✅ Đã gửi STK cho Admin. Bạn vui lòng đợi Admin giải ngân!")

async def admin_pay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("pay_"):
        code = query.data.split("_")[1]
        trade = db.get_trade(code)
        if trade:
            db.update_status(code, STATUS_COMPLETED)
            await query.edit_message_text(f"✅ Đã xác nhận giải ngân thành công cho đơn `{code}`.")
            
            final_msg = (
                f"🎉 **GIAO DỊCH HOÀN TẤT: {code}**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"✅ Admin đã chuyển khoản cho người bán thành công.\n"
                f"🤝 Cảm ơn các bạn đã sử dụng dịch vụ!"
            )
            await telegram_app.bot.send_message(chat_id=trade['group_id'], text=final_msg, parse_mode=ParseMode.MARKDOWN)

# ==========================================================
#                      KHỞI CHẠY
# ==========================================================
async def run_bot():
    telegram_app.add_handler(CommandHandler("start", start_cmd))
    telegram_app.add_handler(CommandHandler("taogdtg", create_trade_cmd))
    telegram_app.add_handler(CommandHandler("done", done_cmd))
    telegram_app.add_handler(CommandHandler("bank", bank_cmd))
    telegram_app.add_handler(CallbackQueryHandler(admin_pay_handler))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(run_bot())
    # Chạy Web Server cho Render và Webhook SePay
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

