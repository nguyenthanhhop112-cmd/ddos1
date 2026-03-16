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
    ContextTypes,
    MessageHandler,
    filters
)

# ==========================================================
#                      CẤU HÌNH NÂNG CAO
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

DB_FILE = "gdtg_ultimate_v7.sqlite3"

# HẰNG SỐ TRẠNG THÁI (Logic System)
ST_WAIT_PAY = "CHO_THANH_TOAN"         
ST_PAID_HOLDING = "BOT_GIU_TIEN"       
ST_BUYER_DONE = "NGUOI_MUA_XAC_NHAN"   
ST_WAIT_PAYOUT = "CHO_ADMIN_CHUYEN"    
ST_COMPLETED = "HOAN_TAT"              
ST_CANCELLED = "DA_HUY"

# ==========================================================
#                      DATABASE ARCHITECTURE
# ==========================================================
class Database:
    def __init__(self):
        with sqlite3.connect(DB_FILE) as conn:
            # Bảng lưu trữ giao dịch
            conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE, group_id INTEGER, group_name TEXT,
                buyer_id INTEGER, buyer_name TEXT, buyer_user TEXT,
                seller_id INTEGER, seller_name TEXT, 
                amount INTEGER, fee INTEGER, total_pay INTEGER,
                product_name TEXT, seller_bank TEXT, status TEXT, 
                qr_msg_id INTEGER, status_msg_id INTEGER,
                created_at TEXT)''')
            
            # Bảng lưu trữ uy tín người dùng
            conn.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                total_trades INTEGER DEFAULT 0,
                total_volume INTEGER DEFAULT 0)''')
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

    def update_msg_ids(self, code, qr_id=None, status_id=None):
        with sqlite3.connect(DB_FILE) as conn:
            if qr_id: conn.execute("UPDATE trades SET qr_msg_id = ? WHERE code = ?", (qr_id, code))
            if status_id: conn.execute("UPDATE trades SET status_msg_id = ? WHERE code = ?", (status_id, code))

    def get_trade(self, code):
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM trades WHERE code = ?", (code,)).fetchone()

    def update_status(self, code, status, bank=None):
        with sqlite3.connect(DB_FILE) as conn:
            if bank: conn.execute("UPDATE trades SET status = ?, seller_bank = ? WHERE code = ?", (status, bank, code))
            else: conn.execute("UPDATE trades SET status = ? WHERE code = ?", (status, code))
            
    def update_user_stats(self, user_id, username, amount):
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("""INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)""", (user_id, username))
            conn.execute("UPDATE users SET total_trades = total_trades + 1, total_volume = total_volume + ? WHERE user_id = ?", (amount, user_id))

db = Database()
app = FastAPI()
tg_app = Application.builder().token(CONFIG["bot_token"]).build()

# ==========================================================
#                      UTILITIES
# ==========================================================
def calc_fee(amount):
    if amount < 100000: return 5000
    if amount < 500000: return 10000
    if amount < 1000000: return 15000
    if amount <= 2000000: return 20000
    return min(int(amount * 0.015), 50000) # Phí 1.5% cho đơn lớn

def get_status_label(status):
    labels = {
        ST_WAIT_PAY: "⏳ Chờ Thanh Toán",
        ST_PAID_HOLDING: "💎 Bot Đang Giữ Tiền",
        ST_BUYER_DONE: "📦 Đã Nhận Hàng",
        ST_WAIT_PAYOUT: "💸 Chờ Giải Ngân",
        ST_COMPLETED: "✅ Hoàn Tất",
        ST_CANCELLED: "❌ Đã Hủy"
    }
    return labels.get(status, "Không rõ")

# ==========================================================
#                      CORE WEBHOOK LOGIC
# ==========================================================
class SePayData(BaseModel):
    content: str
    amountIn: int

@app.post("/webhook")
async def sepay_webhook(data: SePayData, background_tasks: BackgroundTasks):
    match = re.search(r"GD\d+", data.content.upper())
    if match:
        background_tasks.add_task(handle_auto_payment, match.group(), data.amountIn)
    return {"status": "success"}

async def handle_auto_payment(code, amount):
    trade = db.get_trade(code)
    if not trade or trade['status'] != ST_WAIT_PAY: return
    
    if amount >= trade['total_pay']:
        db.update_status(code, ST_PAID_HOLDING)
        
        # 1. Gỡ ghim tin nhắn QR
        try: await tg_app.bot.unpin_chat_message(chat_id=trade['group_id'], message_id=trade['qr_msg_id'])
        except: pass

        # 2. Thông báo nhận tiền chuyên nghiệp
        msg = (
            f"🔔 **THÔNG BÁO NHẬN TIỀN TỰ ĐỘNG**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📝 Mã đơn: `{code}`\n"
            f"💰 Số tiền vào: `{amount:,}đ`\n"
            f"👤 Người mua: {trade['buyer_name']}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🛡 **TRẠNG THÁI:** **BOT ĐÃ GIỮ TIỀN AN TOÀN**\n\n"
            f"🚀 Mời người bán **{trade['seller_name']}** thực hiện giao hàng.\n"
            f"⚠️ Sau khi kiểm tra hàng xong, người mua gõ: `/done {code}`"
        )
        sent = await tg_app.bot.send_message(chat_id=trade['group_id'], text=msg, parse_mode=ParseMode.MARKDOWN)
        db.update_msg_ids(code, status_id=sent.message_id)
        try: await tg_app.bot.pin_chat_message(chat_id=trade['group_id'], message_id=sent.message_id)
        except: pass

        # 3. Báo Admin chi tiết
        adm_msg = (
            f"🏛 **LOG HỆ THỐNG: TIỀN VÀO**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔸 Đơn: `{code}`\n"
            f"🔸 Nhóm: {trade['group_name']}\n"
            f"🔸 Số tiền: {amount:,}đ\n"
            f"🔸 Link nhóm: [Bấm để xem](https://t.me/c/{str(trade['group_id'])[4:]}/{sent.message_id})"
        )
        await tg_app.bot.send_message(chat_id=CONFIG['admin_id'], text=adm_msg, parse_mode=ParseMode.HTML)

# ==========================================================
#                      TELEGRAM INTERFACE
# ==========================================================
async def start(update: Update, context):
    txt = (
        "👋 **Chào mừng bạn đến với Trung Gian Auto V7!**\n\n"
        "Hệ thống trung gian tự động, minh bạch và an toàn tuyệt đối.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📜 **HƯỚNG DẪN DÀNH CHO NHÓM:**\n"
        "🔹 `/taogdtg | giá | SP | @username` : Tạo đơn mới\n"
        "🔹 `/done [mã_đơn]` : Buyer xác nhận đã nhận hàng\n"
        "🔹 `/bank [mã_đơn] [STK]` : Seller gửi thông tin nhận tiền\n"
        "🔹 `/check [mã_đơn]` : Kiểm tra trạng thái đơn hàng\n"
        "🔹 `/profile` : Xem uy tín cá nhân\n\n"
        f"📞 **Support Admin:** {CONFIG['admin_handle']}"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

async def create_trade(update: Update, context):
    if update.effective_chat.type == "private":
        return await update.message.reply_text("❌ Vui lòng sử dụng lệnh này trong Nhóm giao dịch!")

    try:
        raw = update.message.text.split("|")
        if len(raw) < 4: raise ValueError
        
        amount = int(re.sub(r"\D", "", raw[1]))
        product = raw[2].strip()
        seller = raw[3].strip()
        
        code = f"GD{int(datetime.now().timestamp())}"
        fee = calc_fee(amount)
        total = amount + fee
        
        db.create_trade({
            "code": code, "group_id": update.effective_chat.id, "group_name": update.effective_chat.title,
            "buyer_id": update.effective_user.id, "buyer_name": update.effective_user.full_name,
            "buyer_user": f"@{update.effective_user.username}", "seller_name": seller,
            "amount": amount, "fee": fee, "total_pay": total, "product_name": product
        })

        qr = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={total}&addInfo={code}&accountName={CONFIG['bank_owner']}"
        cap = (
            f"🤝 **GIAO DỊCH TRUNG GIAN ĐANG CHỜ**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Mã đơn: `{code}`\n"
            f"📦 **Sản phẩm:** {product}\n"
            f"👤 **Người bán:** {seller}\n"
            f"💰 **Giá:** {amount:,}đ\n"
            f"⚙️ **Phí dịch vụ:** {fee:,}đ\n"
            f"💳 **Tổng thanh toán:** `{total:,}đ`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ **Nội dung CK:** `{code}`\n"
            f"*(Vui lòng bank đúng nội dung để bot tự động nhận diện)*"
        )
        msg = await update.message.reply_photo(photo=qr, caption=cap, parse_mode=ParseMode.MARKDOWN)
        db.update_msg_ids(code, qr_id=msg.message_id)
        try: await tg_app.bot.pin_chat_message(chat_id=update.effective_chat.id, message_id=msg.message_id)
        except: pass
        
    except:
        await update.message.reply_text("❌ **LỖI CÚ PHÁP!**\nVí dụ: `/taogdtg | 500000 | Nick Liên Quân | @admin`")

async def done_trade(update: Update, context):
    if not context.args: return await update.message.reply_text("⚠️ Cú pháp: `/done [mã_đơn]`")
    
    code = context.args[0].upper()
    trade = db.get_trade(code)
    
    if not trade: return await update.message.reply_text("❌ Mã đơn hàng không tồn tại!")
    if update.effective_user.id != trade['buyer_id']:
        return await update.message.reply_text("❌ Chỉ Người Mua mới có quyền sử dụng lệnh này!")
    if trade['status'] != ST_PAID_HOLDING:
        return await update.message.reply_text("❌ Đơn hàng chưa được thanh toán hoặc đã ở trạng thái khác!")

    db.update_status(code, ST_BUYER_DONE)
    
    # Thông báo cho người bán
    txt = (
        f"✅ **XÁC NHẬN HOÀN TẤT GIAO DỊCH**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📦 Người mua đã nhận hàng cho đơn `{code}`.\n"
        f"🔔 Mời người bán **{trade['seller_name']}** cung cấp STK để nhận tiền:\n\n"
        f"👉 Cú pháp: `/bank {code} [Tên Ngân Hàng] [Số Tài Khoản] [Tên Chủ Thẻ]`"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)
    
    # Báo Admin
    await tg_app.bot.send_message(chat_id=CONFIG['admin_id'], text=f"🔔 **Đơn {code}:** Người mua đã bấm DONE. Chờ STK của Seller.")

async def bank_info(update: Update, context):
    if len(context.args) < 2: return await update.message.reply_text("⚠️ Cú pháp: `/bank [mã_đơn] [STK]`")
    
    code = context.args[0].upper()
    info = " ".join(context.args[1:])
    trade = db.get_trade(code)
    
    if not trade or trade['status'] != ST_BUYER_DONE:
        return await update.message.reply_text("❌ Đơn hàng chưa sẵn sàng để nhận STK (Cần Buyer bấm /done trước).")

    db.update_status(code, ST_WAIT_PAYOUT, bank=info)
    
    # Giao diện Admin chuyên nghiệp để giải ngân
    btn = [[InlineKeyboardButton("✅ ĐÃ CHUYỂN KHOẢN XONG", callback_data=f"payout_{code}")]]
    adm_txt = (
        f"🚨 **YÊU CẦU GIẢI NGÂN MỚI**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Mã đơn: `{code}`\n"
        f"📍 Nhóm: {trade['group_name']}\n"
        f"👤 Người bán: {trade['seller_name']}\n"
        f"💰 **Số tiền cần chuyển:** `{trade['amount']:,}đ`\n"
        f"💳 **Thông tin STK:**\n`{info}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Vui lòng chuyển đúng số tiền và nhấn nút xác nhận bên dưới."
    )
    await tg_app.bot.send_message(chat_id=CONFIG['admin_id'], text=adm_txt, reply_markup=InlineKeyboardMarkup(btn), parse_mode=ParseMode.MARKDOWN)
    await update.message.reply_text("✅ **ĐÃ GỬI STK!**\nAdmin sẽ kiểm tra và chuyển tiền cho bạn trong giây lát.")

async def admin_callback(update: Update, context):
    query = update.callback_query
    data = query.data
    
    if data.startswith("payout_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade:
            db.update_status(code, ST_COMPLETED)
            db.update_user_stats(trade['buyer_id'], trade['buyer_user'], trade['amount'])
            
            await query.edit_message_text(f"✅ Đã giải ngân thành công đơn `{code}`.")
            
            # Thông báo hoàn tất vào nhóm
            final_txt = (
                f"🎊 **GIAO DỊCH HOÀN TẤT MỸ MÃN**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🆔 Mã đơn: `{code}`\n"
                f"✅ Admin đã chuyển khoản cho người bán thành công.\n"
                f"🤝 Cảm ơn các bạn đã tin dùng dịch vụ của {CONFIG['admin_handle']}!"
            )
            await tg_app.bot.send_message(chat_id=trade['group_id'], text=final_txt, parse_mode=ParseMode.MARKDOWN)

async def check_trade(update: Update, context):
    if not context.args: return
    code = context.args[0].upper()
    trade = db.get_trade(code)
    if not trade: return await update.message.reply_text("❌ Không tìm thấy đơn hàng này.")
    
    txt = (
        f"📊 **THÔNG TIN ĐƠN HÀNG: {code}**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📦 Sản phẩm: {trade['product_name']}\n"
        f"👤 Buyer: {trade['buyer_name']}\n"
        f"👤 Seller: {trade['seller_name']}\n"
        f"💰 Tổng tiền: {trade['total_pay']:,}đ\n"
        f"🛡 Trạng thái: **{get_status_label(trade['status'])}**"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

# ==========================================================
#                      SYSTEM RUNNER
# ==========================================================
async def main():
    # Đăng ký Handlers
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("taogdtg", create_trade))
    tg_app.add_handler(CommandHandler("done", done_trade))
    tg_app.add_handler(CommandHandler("bank", bank_info))
    tg_app.add_handler(CommandHandler("check", check_trade))
    tg_app.add_handler(CallbackQueryHandler(admin_callback))

    await tg_app.initialize()
    await tg_app.start()
    
    # Polling trong background task
    asyncio.create_task(tg_app.updater.start_polling())

    # Khởi chạy Fast API (Uvicorn)
    port = int(os.environ.get("PORT", 10000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
        
