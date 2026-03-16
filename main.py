import os
import re
import sqlite3
import logging
import asyncio
from datetime import datetime
from typing import Optional

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
    "fee_min": 5000,
    "fee_percent": 0.01
}

DB_FILE = "system_v15.sqlite3"

class Status:
    PENDING = "CHO_THANH_TOAN"
    HOLDING = "BOT_DANG_GIU_TIEN"
    BUYER_DONE = "NGUOI_MUA_XAC_NHAN"
    PAYOUT_WAIT = "CHO_GIAI_NGAN"
    COMPLETED = "THANH_CONG"
    CANCELLED = "DA_HUY"

# ==========================================================
#                      DATABASE ARCHITECTURE
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
                code TEXT UNIQUE, group_id INTEGER, group_name TEXT,
                buyer_id INTEGER, buyer_name TEXT, buyer_user TEXT,
                seller_name TEXT, amount INTEGER, fee INTEGER, total_pay INTEGER,
                product_name TEXT, seller_bank_info TEXT, status TEXT, 
                qr_msg_id INTEGER, status_msg_id INTEGER, created_at TEXT)''')

    def create_trade(self, data):
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
        set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [code]
        with self.conn:
            self.conn.execute(f"UPDATE trades SET {set_clause} WHERE code = ?", values)

db = Database()
app = FastAPI()
tg_app = Application.builder().token(CONFIG["bot_token"]).build()

# ==========================================================
#                      WEBHOOK SEPAY (NHẬN BILL)
# ==========================================================
@app.post("/webhook")
async def sepay_webhook(request: Request):
    try:
        data = await request.json()
        content = data.get("content", "").upper()
        # Đây là số tiền khách vừa chuyển trong cái bill này
        amount_in = int(data.get("amount_in", 0))
        
        match = re.search(r"GD(\d+)", content)
        if match:
            code = f"GD{match.group(1)}"
            logger.info(f"🔔 PHÁT HIỆN BILL MỚI: {code} | Số tiền: {amount_in}")
            asyncio.create_task(process_paid_invoice(code, amount_in))
        
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Lỗi Webhook: {e}")
        return {"status": "error"}

async def process_paid_invoice(code, amount_received):
    trade = db.get_trade(code)
    if not trade or trade['status'] != Status.PENDING:
        return

    # SO SÁNH: Tiền bill nạp vào vs Tiền cần thanh toán của đơn
    if amount_received >= trade['total_pay']:
        # CHUYỂN ĐỦ HOẶC DƯ -> VÀO VIỆC
        db.update_trade(code, status=Status.HOLDING)
        
        try: await tg_app.bot.unpin_chat_message(chat_id=trade['group_id'], message_id=trade['qr_msg_id'])
        except: pass

        msg = f"""<b>✅ GIAO DỊCH {code} ĐÃ NHẬN ĐỦ TIỀN</b>
━━━━━━━━━━━━━━━━━━━━
📦 <b>Sản phẩm:</b> {trade['product_name']}
💰 <b>Số tiền nhận:</b> {amount_received:,} VND
🛡 <b>Trạng thái:</b> BOT ĐANG GIỮ TIỀN AN TOÀN

👤 <b>Người mua:</b> {trade['buyer_name']}
👤 <b>Người bán:</b> {trade['seller_name']}
━━━━━━━━━━━━━━━━━━━━
🚀 <b>YÊU CẦU:</b> Người bán tiến hành giao hàng. Sau khi xong, người mua bấm nút xác nhận dưới đây."""
        
        btn = [[InlineKeyboardButton("✅ TÔI ĐÃ NHẬN ĐỦ HÀNG", callback_data=f"done_{code}")]]
        sent = await tg_app.bot.send_message(chat_id=trade['group_id'], text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(btn))
        db.update_trade(code, status_msg_id=sent.message_id)
        await tg_app.bot.pin_chat_message(chat_id=trade['group_id'], message_id=sent.message_id)
    else:
        # CHUYỂN THIẾU -> BÁO LỖI
        missing = trade['total_pay'] - amount_received
        txt = f"""<b>⚠️ CẢNH BÁO: CHUYỂN THIẾU TIỀN</b>
━━━━━━━━━━━━━━━━━━━━
🆔 <b>Mã đơn:</b> <code>{code}</code>
💰 <b>Cần thanh toán:</b> {trade['total_pay']:,} VND
📥 <b>Thực nhận từ bill:</b> {amount_received:,} VND
❌ <b>CÒN THIẾU:</b> <code>{missing:,}</code> VND

<i>Vui lòng chuyển thêm đúng số tiền thiếu với nội dung chuyển khoản là <code>{code}</code></i>"""
        await tg_app.bot.send_message(chat_id=trade['group_id'], text=txt, parse_mode=ParseMode.HTML)

# ==========================================================
#                      INTERFACE & COMMANDS
# ==========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_info = await context.bot.get_me()
    keyboard = [
        [InlineKeyboardButton("➕ Thêm Bot Vào Nhóm Giao Dịch", url=f"https://t.me/{bot_info.username}?startgroup=true")],
        [InlineKeyboardButton("📖 Hướng Dẫn & Giới Thiệu", callback_data="ui_help")],
        [InlineKeyboardButton("👨‍💻 Liên Hệ Admin", url=f"https://t.me/{CONFIG['admin_handle'][1:]}")]
    ]
    
    txt = f"""<b>⚡ HỆ THỐNG TRUNG GIAN TỰ ĐỘNG CAO CẤP</b>
━━━━━━━━━━━━━━━━━━━━
Chào mừng bạn đến với nền tảng Giao Dịch Trung Gian bảo mật.

<b>💎 ĐẶC ĐIỂM NỔI BẬT:</b>
• <b>Tốc độ:</b> Xác nhận Bank tự động trong 3 giây.
• <b>Minh bạch:</b> Theo dõi trạng thái đơn trực tiếp tại nhóm.
• <b>An toàn:</b> Bot giữ tiền 100%, tránh mọi rủi ro lừa đảo.

<i>Vui lòng chọn chức năng bên dưới để bắt đầu!</i>"""
    
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def cmd_taogdtg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return await update.message.reply_text("❌ Lệnh này chỉ dùng trong Nhóm Giao Dịch!")
    
    try:
        parts = [p.strip() for p in update.message.text.replace("/taogdtg", "").split("|")]
        amount = int(re.sub(r"\D", "", parts[0]))
        product, seller = parts[1], parts[2]
        
        code = f"GD{int(datetime.now().timestamp())}"
        fee = max(CONFIG['fee_min'], int(amount * CONFIG['fee_percent']))
        total = amount + fee

        db.create_trade({
            "code": code, "group_id": update.effective_chat.id, "group_name": update.effective_chat.title,
            "buyer_id": update.effective_user.id, "buyer_name": update.effective_user.full_name,
            "buyer_user": f"@{update.effective_user.username}", "seller_name": seller,
            "amount": amount, "fee": fee, "total_pay": total, "product_name": product
        })

        qr = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={total}&addInfo={code}&accountName={CONFIG['bank_owner'].replace(' ', '%20')}"
        txt = f"<b>🤝 GIAO DỊCH: {code}</b>\n━━━━━━━━━━━━━━━━━━━━\n📦 <b>SP:</b> {product}\n👤 <b>Bán:</b> {seller}\n👤 <b>Mua:</b> {update.effective_user.full_name}\n💵 <b>Giá:</b> {amount:,} VND\n⚙️ <b>Phí:</b> {fee:,} VND\n💳 <b>TỔNG:</b> <code>{total:,}</code> VND\n📝 <b>Nội dung:</b> <code>{code}</code>"

        msg = await update.message.reply_photo(photo=qr, caption=txt, parse_mode=ParseMode.HTML)
        db.update_trade(code, qr_msg_id=msg.message_id)
        try: await msg.pin() 
        except: pass
    except:
        await update.message.reply_text("❌ Sai cú pháp! <code>/taogdtg Tiền | Sản phẩm | @Seller</code>", parse_mode=ParseMode.HTML)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    if data == "ui_help":
        txt = """<b>📖 HƯỚNG DẪN QUY TRÌNH</b>
━━━━━━━━━━━━━━━━━━━━
1. <b>Tạo đơn:</b> Dùng lệnh <code>/taogdtg</code> trong nhóm.
2. <b>Thanh toán:</b> Người mua quét mã QR (Chuyển đúng số tiền).
3. <b>Giao hàng:</b> Người bán giao hàng cho người mua.
4. <b>Xác nhận:</b> Người mua nhấn nút <b>"Tôi đã nhận hàng"</b>.
5. <b>Rút tiền:</b> Người bán dùng lệnh <code>/bank</code> để nhận tiền."""
        await query.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Quay Lại", callback_data="ui_back")]]))

    elif data == "ui_back":
        await cmd_start(update, context)

    elif data.startswith("done_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if not trade or user_id != trade['buyer_id']:
            return await query.answer("⛔ Chỉ người mua mới được bấm!", show_alert=True)
        
        db.update_trade(code, status=Status.BUYER_DONE)
        await query.answer("✅ Xác nhận thành công!")
        txt = f"<b>📦 ĐƠN {code} ĐÃ XONG</b>\nNgười bán {trade['seller_name']} gửi STK rút tiền:\n<code>/bank {code} STK Bank Tên_CT</code>"
        if query.message.photo: await query.edit_message_caption(caption=txt, parse_mode=ParseMode.HTML)
        else: await query.edit_message_text(text=txt, parse_mode=ParseMode.HTML)

    elif data.startswith("adminpayout_"):
        if user_id != CONFIG['admin_id']: return
        code = data.split("_")[1]
        trade = db.get_trade(code)
        db.update_trade(code, status=Status.COMPLETED)
        await query.edit_message_text(f"✅ Đã giải ngân đơn {code}")
        await context.bot.send_message(trade['group_id'], f"<b>🎉 GIAO DỊCH {code} HOÀN TẤT!</b>", parse_mode=ParseMode.HTML)

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2: return
    code, info = context.args[0].upper(), " ".join(context.args[1:])
    trade = db.get_trade(code)
    if trade and trade['status'] == Status.BUYER_DONE:
        db.update_trade(code, status=Status.PAYOUT_WAIT, seller_bank_info=info)
        kb = [[InlineKeyboardButton("✅ XÁC NHẬN ĐÃ BANK", callback_data=f"adminpayout_{code}")]]
        await context.bot.send_message(CONFIG['admin_id'], f"🏛 <b>RÚT TIỀN: {code}</b>\nTiền: {trade['amount']:,}\nSTK: {info}\nNhóm: {trade['group_name']}", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        await update.message.reply_text("✅ Đã gửi yêu cầu cho Admin!")

async def main_runner():
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("taogdtg", cmd_taogdtg))
    tg_app.add_handler(CommandHandler("bank", cmd_bank))
    tg_app.add_handler(CallbackQueryHandler(callback_handler))
    await tg_app.initialize()
    await tg_app.start()
    asyncio.create_task(tg_app.updater.start_polling())
    await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), loop="asyncio")).serve()

if __name__ == "__main__":
    asyncio.run(main_runner())
                                        
