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

DB_FILE = "gdtg_pro_v7.sqlite3"

# Trạng thái logic
ST_WAIT_PAY = "CHO_THANH_TOAN"         # Đợi buyer bank tiền
ST_PAID_HOLDING = "BOT_GIU_TIEN"       # Bot đã nhận tiền từ SePay
ST_BUYER_DONE = "NGUOI_MUA_XAC_NHAN"   # Buyer đã nhận hàng, bấm /done
ST_WAIT_PAYOUT = "CHO_ADMIN_CHUYEN"    # Seller đã gửi STK
ST_COMPLETED = "HOAN_TAT"              # Admin đã bấm nút xác nhận trả tiền

# ==========================================================
#                      DATABASE CORE
# ==========================================================
class Database:
    def __init__(self):
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE, group_id INTEGER, group_name TEXT,
                buyer_id INTEGER, buyer_name TEXT, buyer_user TEXT,
                seller_name TEXT, amount INTEGER, fee INTEGER, total_pay INTEGER,
                product_name TEXT, seller_bank TEXT, status TEXT, created_at TEXT)''')

    def create_trade(self, data):
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("""INSERT INTO trades 
                (code, group_id, group_name, buyer_id, buyer_name, buyer_user, seller_name, 
                 amount, fee, total_pay, product_name, status, created_at) 
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data['code'], data['group_id'], data['group_name'], data['buyer_id'], data['buyer_name'],
                 data['buyer_user'], data['seller_name'], data['amount'], data['fee'],
                 data['total_pay'], data['product_name'], ST_WAIT_PAY, datetime.now().isoformat()))

    def get_trade(self, code):
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM trades WHERE code = ?", (code,)).fetchone()

    def update_status(self, code, status, bank=None):
        with sqlite3.connect(DB_FILE) as conn:
            if bank: conn.execute("UPDATE trades SET status = ?, seller_bank = ? WHERE code = ?", (status, bank, code))
            else: conn.execute("UPDATE trades SET status = ? WHERE code = ?", (status, code))

db = Database()
app = FastAPI()
tg_app = Application.builder().token(CONFIG["bot_token"]).build()

# ==========================================================
#                      TÍNH PHÍ GIAO DỊCH
# ==========================================================
def calc_fee(amount):
    if amount < 100000: return 5000
    if amount < 500000: return 10000
    if amount < 1000000: return 15000
    if amount <= 2000000: return 20000
    return min(int(amount * 0.01), 30000)

# ==========================================================
#                      WEBHOOK SEPAY (TỰ ĐỘNG)
# ==========================================================
class SePayData(BaseModel):
    content: str
    amountIn: int

@app.post("/webhook")
async def sepay_webhook(data: SePayData, background_tasks: BackgroundTasks):
    match = re.search(r"GD\d+", data.content.upper())
    if match:
        background_tasks.add_task(handle_payment, match.group(), data.amountIn)
    return {"status": "ok"}

async def handle_payment(code, amount):
    trade = db.get_trade(code)
    if trade and trade['status'] == ST_WAIT_PAY and amount >= trade['total_pay']:
        db.update_status(code, ST_PAID_HOLDING)
        
        # Thông báo trong nhóm & Ghim tin
        msg = (
            f"✅ **ĐÃ NHẬN TIỀN THÀNH CÔNG**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Mã đơn: `{code}`\n"
            f"💰 Số tiền: {amount:,}đ\n"
            f"👤 Người mua: {trade['buyer_name']}\n\n"
            f"🛡 **TRẠNG THÁI:** Bot đang giữ tiền.\n"
            f"🚀 Mời người bán **{trade['seller_name']}** giao hàng ngay.\n"
            f"💡 Người mua nhận hàng xong hãy gõ: `/done {code}`"
        )
        sent_msg = await tg_app.bot.send_message(chat_id=trade['group_id'], text=msg, parse_mode=ParseMode.MARKDOWN)
        try: await tg_app.bot.pin_chat_message(chat_id=trade['group_id'], message_id=sent_msg.message_id)
        except: pass

        # Thông báo Admin chi tiết
        adm_msg = (
            f"💰 **TIỀN VÀO HỆ THỐNG**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Mã: `{code}` | Nhóm: `{trade['group_name']}`\n"
            f"Thực nhận: `{amount:,}đ`\n"
            f"Buyer: {trade['buyer_name']} ({trade['buyer_user']})"
        )
        await tg_app.bot.send_message(chat_id=CONFIG['admin_id'], text=adm_msg)

# ==========================================================
#                      LỆNH TELEGRAM
# ==========================================================
async def start(update: Update, context):
    txt = (
        "🛡 **HỆ THỐNG TRUNG GIAN AUTO V7**\n"
        "Giao dịch an toàn - Tự động 100%\n\n"
        "📌 **LỆNH SỬ DỤNG:**\n"
        "1. Tạo đơn: `/taogdtg | giá | sản phẩm | @nguoiban`\n"
        "2. Xác nhận: `/done [mã_đơn]`\n"
        "3. Nhận tiền: `/bank [mã_đơn] [STK]`"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

async def create_trade(update: Update, context):
    if update.effective_chat.type == "private": return
    try:
        p = [i.strip() for i in update.message.text.split("|")]
        amt = int(re.sub(r"\D", "", p[1]))
        code = f"GD{int(datetime.now().timestamp())}"
        fee = calc_fee(amt)
        total = amt + fee
        
        db.create_trade({
            "code": code, "group_id": update.effective_chat.id, "group_name": update.effective_chat.title,
            "buyer_id": update.effective_user.id, "buyer_name": update.effective_user.full_name,
            "buyer_user": f"@{update.effective_user.username}", "seller_name": p[3],
            "amount": amt, "fee": fee, "total_pay": total, "product_name": p[2]
        })

        qr = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={total}&addInfo={code}"
        cap = (
            f"🤝 **ĐƠN GIAO DỊCH MỚI: {code}**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📦 **Sản phẩm:** {p[2]}\n"
            f"👤 **Người bán:** {p[3]}\n"
            f"💰 **Giá:** {amt:,}đ | **Phí:** {fee:,}đ\n"
            f"💳 **Tổng cần bank:** `{total:,}`đ\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ **Nội dung bắt buộc:** `{code}`"
        )
        msg = await update.message.reply_photo(photo=qr, caption=cap, parse_mode=ParseMode.MARKDOWN)
        try: await tg_app.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=msg.message_id)
        except: pass
    except: await update.message.reply_text("❌ Lỗi! Cú pháp: `/taogdtg | giá | SP | @nguoiban`")

async def done(update: Update, context):
    if not context.args: return
    code = context.args[0].upper()
    trade = db.get_trade(code)
    if trade and update.effective_user.id == trade['buyer_id'] and trade['status'] == ST_PAID_HOLDING:
        db.update_status(code, ST_BUYER_DONE)
        txt = (
            f"✅ **NGƯỜI MUA ĐÃ XÁC NHẬN**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Mã đơn: `{code}`\n"
            f"🔔 Mời người bán **{trade['seller_name']}** gửi STK để nhận tiền:\n"
            f"👉 Cú pháp: `/bank {code} [Số tài khoản + Ngân hàng]`"
        )
        await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)
        await tg_app.bot.send_message(chat_id=CONFIG['admin_id'], text=f"🔔 Đơn `{code}` đã xong (Buyer bấm /done). Chờ STK...")

async def bank(update: Update, context):
    if len(context.args) < 2: return
    code, info = context.args[0].upper(), " ".join(context.args[1:])
    trade = db.get_trade(code)
    if trade and trade['status'] == ST_BUYER_DONE:
        db.update_status(code, ST_WAIT_PAYOUT, bank=info)
        
        # Báo Admin giải ngân
        btn = [[InlineKeyboardButton("✅ XÁC NHẬN ĐÃ GIẢI NGÂN", callback_data=f"pay_{code}")]]
        adm_txt = (
            f"💸 **YÊU CẦU RÚT TIỀN**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Đơn: `{code}`\n"
            f"💰 Tiền trả Seller: **{trade['amount']:,}đ**\n"
            f"💳 **STK ĐÍCH:** `{info}`\n"
            f"📍 Nhóm: {trade['group_name']}"
        )
        await tg_app.bot.send_message(chat_id=CONFIG['admin_id'], text=adm_txt, reply_markup=InlineKeyboardMarkup(btn))
        await update.message.reply_text("✅ Đã gửi thông tin cho Admin. Vui lòng đợi giải ngân!")

async def admin_callback(update: Update, context):
    q = update.callback_query
    if q.data.startswith("pay_"):
        code = q.data.split("_")[1]
        trade = db.get_trade(code)
        if trade:
            db.update_status(code, ST_COMPLETED)
            await q.edit_message_text(f"✅ Đã giải ngân đơn `{code}`")
            await tg_app.bot.send_message(chat_id=trade['group_id'], text=f"🎉 **GIAO DỊCH {code} HOÀN TẤT!**\nAdmin đã chuyển tiền cho người bán thành công.")

# ==========================================================
#                      RUN SYSTEM
# ==========================================================
async def main():
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("taogdtg", create_trade))
    tg_app.add_handler(CommandHandler("done", done))
    tg_app.add_handler(CommandHandler("bank", bank))
    tg_app.add_handler(CallbackQueryHandler(admin_callback))

    await tg_app.initialize()
    await tg_app.start()
    asyncio.create_task(tg_app.updater.start_polling())

    port = int(os.environ.get("PORT", 10000))
    srv = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio"))
    await srv.serve()

if __name__ == "__main__":
    asyncio.run(main())
                             
