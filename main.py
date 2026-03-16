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

# Sử dụng DB bản V13 để đảm bảo cấu trúc dữ liệu mới nhất
DB_FILE = "system_v13.sqlite3"

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
                code TEXT UNIQUE, 
                group_id INTEGER, 
                group_name TEXT,
                buyer_id INTEGER, 
                buyer_name TEXT, 
                buyer_user TEXT,
                seller_name TEXT, 
                amount INTEGER, 
                fee INTEGER, 
                total_pay INTEGER,
                product_name TEXT, 
                seller_bank_info TEXT, 
                status TEXT, 
                qr_msg_id INTEGER, 
                status_msg_id INTEGER, 
                created_at TEXT)''')

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
#                      WEBHOOK SEPAY (NHẬN TIỀN)
# ==========================================================
@app.post("/webhook")
async def sepay_webhook(request: Request):
    try:
        data = await request.json()
        content = data.get("content", "").upper()
        amount_in = int(data.get("amount_in", 0))
        
        # Regex thông minh để bắt mã đơn GDxxxx giữa các ký tự khác
        match = re.search(r"GD(\d+)", content)
        if match:
            trade_code = f"GD{match.group(1)}"
            logger.info(f"🔔 PHÁT HIỆN BILL: {trade_code} | Số tiền: {amount_in}")
            # Đưa vào hàng chờ xử lý để phản hồi Webhook 200 OK ngay lập tức
            asyncio.create_task(process_paid_invoice(trade_code, amount_in))
        
        return {"status": "success"}
    except Exception as e:
        logger.error(f"❌ Lỗi Webhook: {e}")
        return {"status": "error"}

async def process_paid_invoice(code, amount):
    # Lấy thông tin đơn hàng từ DB để biết đơn này của NHÓM NÀO (group_id)
    trade = db.get_trade(code)
    
    if not trade:
        logger.warning(f"⚠️ Nhận bill {code} nhưng không tìm thấy đơn trong Database.")
        return

    # Chỉ xử lý nếu đơn đang ở trạng thái chờ thanh toán
    if trade['status'] != Status.PENDING:
        logger.info(f"ℹ️ Đơn {code} đã được xử lý trước đó (Trạng thái: {trade['status']}).")
        return

    group_id = trade['group_id']

    # Kiểm tra số tiền khách chuyển
    if amount < trade['total_pay']:
        missing = trade['total_pay'] - amount
        msg_error = f"""<b>⚠️ CẢNH BÁO: CHUYỂN THIẾU TIỀN</b>
━━━━━━━━━━━━━━━━━━━━
🆔 <b>Mã đơn:</b> <code>{code}</code>
💰 <b>Cần thanh toán:</b> <code>{trade['total_pay']:,}</code> VND
📥 <b>Thực nhận:</b> <code>{amount:,}</code> VND
❌ <b>CÒN THIẾU:</b> <code>{missing:,}</code> VND

👤 <b>Người mua:</b> {trade['buyer_name']}
⚠️ <i>Vui lòng chuyển thêm đúng số tiền thiếu với nội dung <code>{code}</code> để hệ thống tự động xác nhận!</i>"""
        await tg_app.bot.send_message(chat_id=group_id, text=msg_error, parse_mode=ParseMode.HTML)
        return

    # CẬP NHẬT TRẠNG THÁI: BOT ĐÃ GIỮ TIỀN
    db.update_trade(code, status=Status.HOLDING)
    
    # Gỡ ghim mã QR cũ
    try:
        await tg_app.bot.unpin_chat_message(chat_id=group_id, message_id=trade['qr_msg_id'])
    except: pass

    # Thông báo nhận đủ tiền cho NHÓM CỤ THỂ
    msg_success = f"""<b>✅ HỆ THỐNG ĐÃ NHẬN ĐỦ TIỀN {code}</b>
━━━━━━━━━━━━━━━━━━━━
📦 <b>Sản phẩm:</b> {trade['product_name']}
💰 <b>Số tiền nhận:</b> {amount:,} VND
🛡 <b>Trạng thái:</b> BOT ĐANG GIỮ TIỀN AN TOÀN

👤 <b>Người mua:</b> {trade['buyer_name']}
👤 <b>Người bán:</b> {trade['seller_name']}
━━━━━━━━━━━━━━━━━━━━
🚀 <b>YÊU CẦU:</b> Người bán hãy giao hàng cho người mua ngay.
⚠️ <b>GHI CHÚ:</b> Sau khi nhận đủ hàng, người mua <b>PHẢI</b> bấm nút xác nhận dưới đây."""

    btn = [[InlineKeyboardButton("✅ TÔI ĐÃ NHẬN ĐỦ HÀNG", callback_data=f"done_{code}")]]
    
    try:
        sent_msg = await tg_app.bot.send_message(
            chat_id=group_id, 
            text=msg_success, 
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(btn)
        )
        db.update_trade(code, status_msg_id=sent_msg.message_id)
        await tg_app.bot.pin_chat_message(chat_id=group_id, message_id=sent_msg.message_id)
    except Exception as e:
        logger.error(f"❌ Lỗi gửi tin vào nhóm {group_id}: {e}")

# ==========================================================
#                      BOT COMMANDS
# ==========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ Thêm Bot Vào Nhóm", url=f"https://t.me/{(await context.bot.get_me()).username}?startgroup=true")],
        [InlineKeyboardButton("👨‍💻 Admin", url=f"https://t.me/{CONFIG['admin_handle'][1:]}")]
    ]
    txt = "<b>⚡ HỆ THỐNG TRUNG GIAN AUTO V13</b>\nBot tự động nhận diện thanh toán và bảo vệ giao dịch đa nhóm."
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

async def cmd_taogdtg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return await update.message.reply_text("❌ Lệnh này chỉ dùng trong Nhóm Giao Dịch!")
    
    try:
        # Tách lệnh: /taogdtg 100000 | Nick game | @seller
        text = update.message.text.replace("/taogdtg", "").strip()
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 3: raise ValueError()
        
        amount = int(re.sub(r"\D", "", parts[0]))
        product = parts[1]
        seller = parts[2]
        
        # Tạo mã đơn duy nhất kèm timestamp
        code = f"GD{int(datetime.now().timestamp())}"
        fee = max(CONFIG['fee_min'], int(amount * CONFIG['fee_percent']))
        total = amount + fee

        # LƯU THÔNG TIN NHÓM VÀ NGƯỜI DÙNG VÀO DB
        db.create_trade({
            "code": code, 
            "group_id": update.effective_chat.id, 
            "group_name": update.effective_chat.title,
            "buyer_id": update.effective_user.id, 
            "buyer_name": update.effective_user.full_name,
            "buyer_user": f"@{update.effective_user.username}", 
            "seller_name": seller,
            "amount": amount, "fee": fee, "total_pay": total, "product_name": product
        })

        qr_url = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={total}&addInfo={code}&accountName={CONFIG['bank_owner'].replace(' ', '%20')}"
        
        txt = f"""<b>🤝 GIAO DỊCH TRUNG GIAN MỚI</b>
━━━━━━━━━━━━━━━━━━━━
🆔 <b>Mã đơn:</b> <code>{code}</code>
📦 <b>Sản phẩm:</b> {product}
👤 <b>Người bán:</b> {seller}
👤 <b>Người mua:</b> {update.effective_user.full_name}
━━━━━━━━━━━━━━━━━━━━
💵 <b>Giá trị:</b> {amount:,} VND
⚙️ <b>Phí:</b> {fee:,} VND
💳 <b>TỔNG THANH TOÁN:</b> <code>{total:,}</code> VND
📝 <b>NỘI DUNG CK:</b> <code>{code}</code>

⚠️ <i>Vui lòng chuyển đúng số tiền và nội dung. Hệ thống tự động xác nhận trong 3s!</i>"""

        msg = await update.message.reply_photo(photo=qr_url, caption=txt, parse_mode=ParseMode.HTML)
        db.update_trade(code, qr_msg_id=msg.message_id)
        try: await msg.pin() 
        except: pass
        
    except:
        await update.message.reply_text("❌ <b>Sai cú pháp!</b>\n👉 <code>/taogdtg Số_tiền | Tên_SP | @Người_bán</code>", parse_mode=ParseMode.HTML)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    if data.startswith("done_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        
        if not trade: return await query.answer("❌ Đơn không tồn tại!")
        if user_id != trade['buyer_id']:
            return await query.answer("⛔ Chỉ người mua mới được xác nhận!", show_alert=True)
            
        if trade['status'] != Status.HOLDING:
            return await query.answer("⚠️ Trạng thái đơn không hợp lệ!")

        db.update_trade(code, status=Status.BUYER_DONE)
        await query.answer("✅ Xác nhận thành công!")
        
        # Cập nhật thông báo trong nhóm
        txt_done = f"""<b>📦 XÁC NHẬN HOÀN TẤT GIAO HÀNG</b>
━━━━━━━━━━━━━━━━━━━━
🆔 <b>Mã đơn:</b> <code>{code}</code>
✅ Người mua đã nhận đủ hàng.

🔔 <b>Người bán {trade['seller_name']} dùng lệnh rút tiền:</b>
<code>/bank {code} STK Tên_Bank Chủ_TK</code>"""
        
        if query.message.photo:
            await query.edit_message_caption(caption=txt_done, parse_mode=ParseMode.HTML)
        else:
            await query.edit_message_text(text=txt_done, parse_mode=ParseMode.HTML)

    elif data.startswith("adminpayout_"):
        if user_id != CONFIG['admin_id']: return
        code = data.split("_")[1]
        trade = db.get_trade(code)
        
        if trade and trade['status'] == Status.PAYOUT_WAIT:
            db.update_trade(code, status=Status.COMPLETED)
            await query.answer("✅ Đã chốt đơn!")
            await query.edit_message_text(f"✅ Đã giải ngân thành công đơn <code>{code}</code>", parse_mode=ParseMode.HTML)
            
            # Gửi tin nhắn về nhóm ban đầu của đơn hàng này
            await context.bot.send_message(
                chat_id=trade['group_id'], 
                text=f"<b>🎉 GIAO DỊCH {code} HOÀN TẤT TUYỆT ĐỐI!</b>\nAdmin đã chuyển tiền cho người bán. Cảm ơn các bạn!", 
                parse_mode=ParseMode.HTML
            )

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2: return
    
    code = context.args[0].upper()
    bank_info = " ".join(context.args[1:])
    trade = db.get_trade(code)
    
    if not trade or trade['status'] != Status.BUYER_DONE:
        return await update.message.reply_text("❌ Đơn chưa sẵn sàng giải ngân!")

    db.update_trade(code, status=Status.PAYOUT_WAIT, seller_bank_info=bank_info)
    
    # Gửi yêu cầu cho Admin kèm thông tin nhóm
    adm_kb = [[InlineKeyboardButton("✅ XÁC NHẬN ĐÃ BANK", callback_data=f"adminpayout_{code}")]]
    adm_txt = f"""<b>🏛 YÊU CẦU GIẢI NGÂN</b>
━━━━━━━━━━━━━━━━━━━━
🆔 Mã đơn: <code>{code}</code>
💰 Số tiền: <code>{trade['amount']:,}</code> VND
💳 STK: <code>{bank_info}</code>
📍 Nhóm: {trade['group_name']}"""
    
    await context.bot.send_message(chat_id=CONFIG['admin_id'], text=adm_txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(adm_kb))
    await update.message.reply_text("✅ Đã gửi yêu cầu rút tiền cho Admin!")

# ==========================================================
#                      KHỞI CHẠY
# ==========================================================
async def main_runner():
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("taogdtg", cmd_taogdtg))
    tg_app.add_handler(CommandHandler("bank", cmd_bank))
    tg_app.add_handler(CallbackQueryHandler(callback_handler))

    await tg_app.initialize()
    await tg_app.start()
    asyncio.create_task(tg_app.updater.start_polling())

    port = int(os.environ.get("PORT", 10000))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio"))
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main_runner())
        
