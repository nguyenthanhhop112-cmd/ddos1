import os
import re
import sqlite3
import logging
import asyncio
from datetime import datetime
from typing import Optional, List

import uvicorn
from fastapi import FastAPI, BackgroundTasks, Request
from pydantic import BaseModel
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    ReplyKeyboardMarkup,
    WebAppInfo
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

# Cấu hình Logging chuyên nghiệp
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
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
    "fee_percent": 0.01  # 1% phí giao dịch
}

DB_FILE = "gdtg_omni_v8.sqlite3"

# ĐỊNH NGHĨA TRẠNG THÁI (State Machine)
class Status:
    PENDING = "WAITING_PAYMENT"
    HOLDING = "BOT_HOLDING_MONEY"
    DELIVERING = "SELLER_DELIVERING"
    BUYER_CONFIRMED = "BUYER_DONE"
    PAYOUT_WAIT = "WAITING_ADMIN_PAYOUT"
    COMPLETED = "SUCCESSFUL"
    CANCELLED = "CANCELLED"

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
            # Bảng giao dịch
            self.conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE, group_id INTEGER, group_name TEXT,
                buyer_id INTEGER, buyer_name TEXT, buyer_user TEXT,
                seller_id INTEGER, seller_name TEXT, 
                amount INTEGER, fee INTEGER, total_pay INTEGER,
                product_name TEXT, seller_bank_info TEXT, status TEXT, 
                qr_msg_id INTEGER, status_msg_id INTEGER, created_at TEXT)''')
            
            # Bảng người dùng & uy tín
            self.conn.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
                completed_trades INTEGER DEFAULT 0, total_volume INTEGER DEFAULT 0,
                reputation_score INTEGER DEFAULT 100)''')

    def save_trade(self, data):
        with self.conn:
            self.conn.execute("""INSERT INTO trades 
                (code, group_id, group_name, buyer_id, buyer_name, buyer_user, seller_name, 
                 amount, fee, total_pay, product_name, status, created_at) 
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data['code'], data['group_id'], data['group_name'], data['buyer_id'], data['buyer_name'],
                 data['buyer_user'], data['seller_name'], data['amount'], data['fee'],
                 data['total_pay'], data['product_name'], Status.PENDING, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    def get_trade(self, code):
        return self.conn.execute("SELECT * FROM trades WHERE code = ?", (code,)).fetchone()

    def update_trade(self, code, **kwargs):
        query = "UPDATE trades SET " + ", ".join([f"{k} = ?" for k in kwargs.keys()]) + " WHERE code = ?"
        params = list(kwargs.values()) + [code]
        with self.conn: self.conn.execute(query, params)

    def update_user_stats(self, user_id, name, username, amount):
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?,?,?)", (user_id, username, name))
            self.conn.execute("UPDATE users SET completed_trades = completed_trades + 1, total_volume = total_volume + ? WHERE user_id = ?", (amount, user_id))

db = Database()

# ==========================================================
#                      API & WEBHOOK (SEPAY)
# ==========================================================
app = FastAPI()

class SePayWebhook(BaseModel):
    id: int
    gateway: str
    amount_in: int
    amount_out: int
    content: str
    code: Optional[str] = None
    transaction_date: str
    account_number: str

@app.post("/webhook")
async def sepay_handler(data: SePayWebhook, background_tasks: BackgroundTasks):
    logger.info(f"Nhận Webhook: {data.content} | Số tiền: {data.amount_in}")
    # Tìm mã GD trong nội dung chuyển khoản
    match = re.search(r"GD\d+", data.content.upper())
    if match:
        code = match.group()
        background_tasks.add_task(process_payment, code, data.amount_in)
    return {"status": "success"}

async def process_payment(code, amount):
    trade = db.get_trade(code)
    if not trade or trade['status'] != Status.PENDING: return
    if amount < trade['total_pay']: return

    db.update_trade(code, status=Status.HOLDING)
    
    # Gỡ QR cũ
    try: await tg_app.bot.unpin_chat_message(chat_id=trade['group_id'], message_id=trade['qr_msg_id'])
    except: pass

    # Nút bấm cho Buyer
    btn = [[InlineKeyboardButton("✅ XÁC NHẬN ĐÃ NHẬN HÀNG", callback_data=f"done_{code}")]]
    
    msg = (
        f"💰 **XÁC NHẬN: ĐÃ NHẬN `{amount:,}đ`**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📦 Đơn hàng: `{code}`\n"
        f"🛡 Trạng thái: **Bot đang giữ tiền an toàn**\n\n"
        f"🚀 Mời người bán **{trade['seller_name']}** bàn giao sản phẩm.\n"
        f"⚠️ Sau khi nhận hàng, người mua bấm nút dưới đây để xác nhận."
    )
    sent = await tg_app.bot.send_message(
        chat_id=trade['group_id'], 
        text=msg, 
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(btn)
    )
    db.update_trade(code, status_msg_id=sent.message_id)
    try: await tg_app.bot.pin_chat_message(chat_id=trade['group_id'], message_id=sent.message_id)
    except: pass

# ==========================================================
#                      BOT LOGIC & UI
# ==========================================================
tg_app = Application.builder().token(CONFIG["bot_token"]).build()

async def cmd_start(update: Update, context):
    bot_user = await context.bot.get_me()
    keyboard = [
        [InlineKeyboardButton("➕ Thêm vào Nhóm", url=f"https://t.me/{bot_user.username}?startgroup=true")],
        [InlineKeyboardButton("📜 Hướng dẫn", callback_data="ui_help"), InlineKeyboardButton("📊 Uy tín", callback_data="ui_profile")],
        [InlineKeyboardButton("👨‍💻 Admin Support", url=f"https://t.me/{CONFIG['admin_handle'][1:]}")]
    ]
    welcome_text = (
        "👋 **Chào mừng bạn đến với OMNI GDTG V8!**\n\n"
        "Hệ thống trung gian tự động hóa chuyên nghiệp nhất dành cho các Group mua bán Telegram.\n\n"
        "✅ **Auto Bank 24/7**\n"
        "✅ **Giao diện nút bấm trực quan**\n"
        "✅ **Bảo mật dữ liệu tuyệt đối**"
    )
    await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def cmd_create_trade(update: Update, context):
    if update.effective_chat.type == "private":
        return await update.message.reply_text("❌ Lệnh này chỉ dùng trong nhóm giao dịch!")
    
    try:
        # Cấu trúc: /taogdtg 500000 | Tên SP | @seller
        parts = [p.strip() for p in update.message.text.split("|")]
        amount = int(re.sub(r"\D", "", parts[0]))
        product = parts[1]
        seller = parts[2]
        
        code = f"GD{int(datetime.now().timestamp())}"
        fee = max(CONFIG['fee_min'], int(amount * CONFIG['fee_percent']))
        total = amount + fee

        trade_data = {
            "code": code, "group_id": update.effective_chat.id, "group_name": update.effective_chat.title,
            "buyer_id": update.effective_user.id, "buyer_name": update.effective_user.full_name,
            "buyer_user": f"@{update.effective_user.username or 'NoUser'}", "seller_name": seller,
            "amount": amount, "fee": fee, "total_pay": total, "product_name": product
        }
        db.save_trade(trade_data)

        qr_url = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={total}&addInfo={code}"
        
        caption = (
            f"🤝 **GIAO DỊCH ĐANG CHỜ THANH TOÁN**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Mã đơn: `{code}`\n"
            f"📦 Sản phẩm: **{product}**\n"
            f"👤 Người bán: {seller}\n"
            f"💰 Tiền hàng: `{amount:,}đ`\n"
            f"⚙️ Phí dịch vụ: `{fee:,}đ`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💳 Tổng tiền: `{total:,}đ`\n"
            f"📝 Nội dung CK: `{code}`"
        )
        
        btn = [[InlineKeyboardButton("❌ Hủy giao dịch (Admin)", callback_data=f"cancel_{code}")]]
        msg = await update.message.reply_photo(photo=qr_url, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(btn))
        db.update_trade(code, qr_msg_id=msg.message_id)
        try: await msg.pin()
        except: pass
        
    except Exception:
        await update.message.reply_text("❌ **Sai cú pháp!**\nSử dụng: `/taogdtg giá | sản phẩm | @nguoiban` \n(Lưu ý có dấu gạch đứng `|`)")

async def callback_handler(update: Update, context):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    # Xử lý nút DONE (Xác nhận nhận hàng)
    if data.startswith("done_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade and user_id == trade['buyer_id'] and trade['status'] == Status.HOLDING:
            db.update_trade(code, status=Status.BUYER_CONFIRMED)
            try: await context.bot.unpin_chat_message(chat_id=trade['group_id'], message_id=trade['status_msg_id'])
            except: pass
            
            txt = (
                f"✅ **BÊN MUA ĐÃ XÁC NHẬN XONG!**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🆔 Đơn: `{code}`\n"
                f"🔔 Mời người bán **{trade['seller_name']}** bấm vào nút dưới đây để cung cấp STK nhận tiền."
            )
            btn = [[InlineKeyboardButton("🏦 CUNG CẤP STK NHẬN TIỀN", callback_data=f"inputbank_{code}")]]
            await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(btn), parse_mode=ParseMode.MARKDOWN)

    # Xử lý nút Input Bank
    elif data.startswith("inputbank_"):
        code = data.split("_")[1]
        await query.answer("Vui lòng gõ lệnh: /bank " + code + " [STK + Ngân hàng]", show_alert=True)

    # Xử lý nút Payout (Cho Admin)
    elif data.startswith("payout_"):
        if user_id != CONFIG['admin_id']: return
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade:
            db.update_trade(code, status=Status.COMPLETED)
            db.update_user_stats(trade['buyer_id'], trade['buyer_name'], trade['buyer_user'], trade['amount'])
            await query.edit_message_text(f"✅ Đơn `{code}` đã giải ngân thành công!")
            await tg_app.bot.send_message(chat_id=trade['group_id'], text=f"🎊 **GIAO DỊCH {code} HOÀN TẤT!**\nAdmin đã giải ngân tiền cho người bán. Cảm ơn các bạn!")

    # UI Chức năng phụ
    elif data == "ui_help":
        await query.edit_message_text("📖 **Hướng dẫn:**\n1. Tạo đơn bằng lệnh `/taogdtg`.\n2. Buyer bank theo QR.\n3. Seller giao hàng.\n4. Buyer bấm nút 'Xác nhận'.\n5. Seller gửi STK nhận tiền.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="back_home")]]))
    
    elif data == "back_home":
        # (Viết lại giao diện start)
        pass

async def cmd_bank(update: Update, context):
    if len(context.args) < 2: return
    code = context.args[0].upper()
    bank_info = " ".join(context.args[1:])
    trade = db.get_trade(code)
    
    if trade and trade['status'] == Status.BUYER_CONFIRMED:
        db.update_trade(code, status=Status.PAYOUT_WAIT, seller_bank_info=bank_info)
        
        # Gửi cho Admin duyệt
        adm_btn = [[InlineKeyboardButton("✅ XÁC NHẬN ĐÃ PAYOUT", callback_data=f"payout_{code}")]]
        adm_txt = (
            f"💸 **YÊU CẦU GIẢI NGÂN**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 Đơn: `{code}`\n"
            f"💰 Tiền hàng: `{trade['amount']:,}đ`\n"
            f"💳 STK Đích: `{bank_info}`\n"
            f"📍 Nhóm: {trade['group_name']}"
        )
        await tg_app.bot.send_message(chat_id=CONFIG['admin_id'], text=adm_txt, reply_markup=InlineKeyboardMarkup(adm_btn))
        await update.message.reply_text("✅ Đã gửi STK cho Admin. Bạn sẽ nhận được tiền trong giây lát!")

# ==========================================================
#                      KHỞI CHẠY HỆ THỐNG
# ==========================================================
async def main_runner():
    # Đăng ký Handlers
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("taogdtg", cmd_create_trade))
    tg_app.add_handler(CommandHandler("bank", cmd_bank))
    tg_app.add_handler(CallbackQueryHandler(callback_handler))

    await tg_app.initialize()
    await tg_app.start()
    asyncio.create_task(tg_app.updater.start_polling())

    # Chạy Web Server song song
    port = int(os.environ.get("PORT", 10000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main_runner())
    except (KeyboardInterrupt, SystemExit):
        pass
    
