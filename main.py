import os
import re
import sqlite3
import logging
import asyncio
import time
from datetime import datetime
from typing import Optional, Dict

import uvicorn
from fastapi import FastAPI, Request
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
    "aml_note": "⚠️ <b>LƯU Ý:</b> Hệ thống nghiêm cấm rửa tiền. Tiền bẩn sẽ bị phong tỏa và báo cáo cơ quan chức năng.",
    "spam_delay": 2.0 # Giây giữa các lần gõ lệnh
}

DB_FILE = "system_v16_final.sqlite3"
user_cooldowns: Dict[int, float] = {}

class Status:
    PENDING = "⏳ CHỜ THANH TOÁN"
    HOLDING = "🛡 BOT GIỮ TIỀN"
    BUYER_DONE = "📦 KHÁCH NHẬN HÀNG"
    PAYOUT_WAIT = "🏛 CHỜ GIẢI NGÂN"
    COMPLETED = "✅ THÀNH CÔNG"
    CANCELLED = "❌ ĐÃ HỦY"

# ==========================================================
#                      HÀM TÍNH PHÍ (FEE LOGIC)
# ==========================================================
def calculate_fee(amount: int) -> int:
    if amount < 100000:
        return 5000
    elif amount < 500000:
        return 10000
    elif amount < 1000000:
        return 15000
    elif amount <= 2000000:
        return 20000
    else:
        return 30000

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
            # Bảng giao dịch
            self.conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE, group_id INTEGER, group_name TEXT, group_link TEXT,
                buyer_id INTEGER, buyer_name TEXT, buyer_user TEXT,
                seller_name TEXT, amount INTEGER, fee INTEGER, total_pay INTEGER,
                product_name TEXT, seller_bank_info TEXT, status TEXT, 
                qr_msg_id INTEGER, status_msg_id INTEGER, created_at TEXT)''')
            
            # Bảng logs lịch sử
            self.conn.execute('''CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT, action TEXT, detail TEXT, timestamp TEXT)''')

    def create_trade(self, data):
        with self.conn:
            self.conn.execute("""INSERT INTO trades 
                (code, group_id, group_name, group_link, buyer_id, buyer_name, buyer_user, seller_name, 
                 amount, fee, total_pay, product_name, status, created_at) 
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data['code'], data['group_id'], data['group_name'], data['group_link'], data['buyer_id'], data['buyer_name'],
                 data['buyer_user'], data['seller_name'], data['amount'], data['fee'],
                 data['total_pay'], data['product_name'], Status.PENDING, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    def add_log(self, code, action, detail):
        with self.conn:
            self.conn.execute("INSERT INTO logs (code, action, detail, timestamp) VALUES (?,?,?,?)",
                             (code, action, detail, datetime.now().strftime("%H:%M:%S %d/%m/%Y")))

    def get_trade(self, code):
        return self.conn.execute("SELECT * FROM trades WHERE code = ?", (code,)).fetchone()

    def update_trade(self, code, **kwargs):
        set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [code]
        with self.conn:
            self.conn.execute(f"UPDATE trades SET {set_clause} WHERE code = ?", values)

    def get_stats(self):
        with self.conn:
            return self.conn.execute("SELECT COUNT(*), SUM(amount), SUM(fee) FROM trades WHERE status = ?", (Status.COMPLETED,)).fetchone()

db = Database()
app = FastAPI()
tg_app = Application.builder().token(CONFIG["bot_token"]).build()

# ==========================================================
#                      CHỐNG SPAM
# ==========================================================
def check_spam(user_id: int) -> bool:
    now = time.time()
    last = user_cooldowns.get(user_id, 0)
    if now - last < CONFIG["spam_delay"]:
        return True
    user_cooldowns[user_id] = now
    return False

# ==========================================================
#                      WEBHOOK NHẬN TIỀN
# ==========================================================
@app.post("/webhook")
async def sepay_webhook(request: Request):
    try:
        data = await request.json()
        content = str(data.get("content", "")).upper()
        raw_val = data.get("amount_in") or data.get("amount") or "0"
        amount_in = int(re.sub(r"\D", "", str(raw_val)))
        
        match = re.search(r"GD(\d+)", content)
        if match:
            code = f"GD{match.group(1)}"
            asyncio.create_task(process_paid(code, amount_in))
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def process_paid(code, amount_received):
    trade = db.get_trade(code)
    if not trade or trade['status'] != Status.PENDING: return

    if amount_received >= int(trade['total_pay']):
        db.update_trade(code, status=Status.HOLDING)
        db.add_log(code, "THANH TOÁN", f"Nhận đủ {amount_received:,}đ")
        
        try: await tg_app.bot.unpin_chat_message(trade['group_id'], trade['qr_msg_id'])
        except: pass

        msg = f"""<b>✅ ĐÃ NHẬN TIỀN - ĐƠN {code}</b>
━━━━━━━━━━━━━━━━━━━━
💰 <b>Số tiền:</b> {amount_received:,} VND
📦 <b>Sản phẩm:</b> {trade['product_name']}
🛡 <b>Trạng thái:</b> BOT ĐANG GIỮ TIỀN AN TOÀN

👤 <b>Người bán:</b> {trade['seller_name']}
━━━━━━━━━━━━━━━━━━━━
🚀 <b>YÊU CẦU:</b> Người bán giao hàng ngay. Sau khi xong, người mua bấm nút xác nhận bên dưới."""
        
        kb = [[InlineKeyboardButton("✅ TÔI ĐÃ NHẬN ĐỦ HÀNG", callback_data=f"done_{code}")]]
        sent = await tg_app.bot.send_message(trade['group_id'], msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        db.update_trade(code, status_msg_id=sent.message_id)
        try: await tg_app.bot.pin_chat_message(trade['group_id'], sent.message_id)
        except: pass

# ==========================================================
#                      COMMANDS HANDLER
# ==========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if check_spam(update.effective_user.id): return
    kb = [
        [InlineKeyboardButton("➕ Thêm Vào Nhóm", url=f"https://t.me/{(await context.bot.get_me()).username}?startgroup=true")],
        [InlineKeyboardButton("📖 Hướng Dẫn", callback_data="ui_help"), InlineKeyboardButton("📊 Thống Kê", callback_data="ui_stats")],
        [InlineKeyboardButton("👨‍💻 Admin", url=f"https://t.me/{CONFIG['admin_handle'][1:]}")]
    ]
    txt = f"<b>⚡ HỆ THỐNG TRUNG GIAN AUTO V16</b>\n━━━━━━━━━━━━━━━━━━━━\nAn toàn - Minh bạch - Tự động 100%\n\n{CONFIG['aml_note']}"
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

async def cmd_taogdtg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private": return
    if check_spam(update.effective_user.id): return

    try:
        raw = update.message.text.replace("/taogdtg", "").strip()
        parts = [p.strip() for p in raw.split("|")]
        amount = int(re.sub(r"\D", "", parts[0]))
        product = parts[1]
        seller = parts[2]
        
        code = f"GD{int(datetime.now().timestamp())}"
        fee = calculate_fee(amount)
        total = amount + fee

        try: link = await update.effective_chat.export_invite_link()
        except: link = "Không lấy được link (Thiếu quyền Admin)"

        db.create_trade({
            "code": code, "group_id": update.effective_chat.id, "group_name": update.effective_chat.title,
            "group_link": link, "buyer_id": update.effective_user.id, "buyer_name": update.effective_user.full_name,
            "buyer_user": f"@{update.effective_user.username}", "seller_name": seller,
            "amount": amount, "fee": fee, "total_pay": total, "product_name": product
        })
        db.add_log(code, "TẠO ĐƠN", f"Mua: {update.effective_user.full_name} | Bán: {seller}")

        qr = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={total}&addInfo={code}&accountName={CONFIG['bank_owner'].replace(' ', '%20')}"
        txt = f"""<b>🤝 ĐƠN GIAO DỊCH MỚI: {code}</b>
━━━━━━━━━━━━━━━━━━━━
📦 <b>Sản phẩm:</b> {product}
👤 <b>Người Bán:</b> {seller}
👤 <b>Người Mua:</b> {update.effective_user.full_name}
━━━━━━━━━━━━━━━━━━━━
💵 <b>Tiền hàng:</b> {amount:,} VND
⚙️ <b>Phí GD:</b> {fee:,} VND
💳 <b>TỔNG THANH TOÁN:</b> <code>{total:,}</code> VND
📝 <b>Nội dung CK:</b> <code>{code}</code>

{CONFIG['aml_note']}"""

        kb = [[InlineKeyboardButton("🔄 Lấy QR", callback_data=f"getqr_{code}"), InlineKeyboardButton("❌ Hủy Đơn", callback_data=f"cancel_{code}")]]
        msg = await update.message.reply_photo(photo=qr, caption=txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        db.update_trade(code, qr_msg_id=msg.message_id)
        try: await msg.pin()
        except: pass
    except:
        await update.message.reply_text("❌ <b>Sai cú pháp!</b>\nSử dụng: <code>/taogdtg Tiền | Sản phẩm | @Seller</code>", parse_mode=ParseMode.HTML)

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if check_spam(update.effective_user.id): return
    if len(context.args) < 2: return await update.message.reply_text("❌ Cú pháp: <code>/bank [MãGD] [STK Bank Tên]</code>", parse_mode=ParseMode.HTML)

    code, info = context.args[0].upper(), " ".join(context.args[1:])
    trade = db.get_trade(code)
    if not trade: return await update.message.reply_text("❌ Đơn không tồn tại!")

    user_tag = f"@{update.effective_user.username}".lower()
    if user_tag != trade['seller_name'].lower():
        return await update.message.reply_text(f"⛔ Chỉ người bán (<b>{trade['seller_name']}</b>) mới có quyền rút tiền!", parse_mode=ParseMode.HTML)

    if trade['status'] == Status.BUYER_DONE:
        db.update_trade(code, status=Status.PAYOUT_WAIT, seller_bank_info=info)
        db.add_log(code, "YÊU CẦU RÚT", info)

        kb = [[InlineKeyboardButton("✅ XÁC NHẬN ĐÃ BANK", callback_data=f"adminpayout_{code}")]]
        admin_msg = f"🏛 <b>YÊU CẦU RÚT TIỀN: {code}</b>\n━━━━━━━━━━━━━━━━━━━━\n💰 Tiền hàng: {trade['amount']:,} VND\n💳 STK: {info}\n👥 Seller: {trade['seller_name']}\n📂 Nhóm: {trade['group_name']}\n🔗 Link: {trade['group_link']}"
        await context.bot.send_message(CONFIG['admin_id'], admin_msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        await update.message.reply_text("✅ <b>Gửi yêu cầu thành công!</b>\nAdmin đang kiểm tra và giải ngân cho bạn.")
    else:
        await update.message.reply_text(f"❌ Trạng thái đơn không hợp lệ: {trade['status']}")

# ==========================================================
#                      CALLBACK HANDLERS
# ==========================================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    username = f"@{update.effective_user.username}".lower()

    if data.startswith("done_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade and user_id == trade['buyer_id']:
            db.update_trade(code, status=Status.BUYER_DONE)
            db.add_log(code, "XÁC NHẬN", "Khách đã nhận hàng")
            txt = f"<b>📦 ĐƠN {code} HOÀN TẤT</b>\n\nNgười bán {trade['seller_name']} rút tiền bằng cách gõ:\n<code>/bank {code} [STK Tên Ngân Hàng]</code>"
            if query.message.photo: await query.edit_message_caption(caption=txt, parse_mode=ParseMode.HTML)
            else: await query.edit_message_text(text=txt, parse_mode=ParseMode.HTML)
        else:
            await query.answer("⛔ Bạn không phải người mua đơn này!", show_alert=True)

    elif data.startswith("adminpayout_"):
        if user_id != CONFIG['admin_id']: return
        code = data.split("_")[1]
        trade = db.get_trade(code)
        db.update_trade(code, status=Status.COMPLETED)
        db.add_log(code, "HOÀN TẤT", "Admin đã giải ngân")
        await query.edit_message_text(f"✅ Đã giải ngân đơn {code}")
        await context.bot.send_message(trade['group_id'], f"<b>🎉 GIAO DỊCH {code} ĐÃ KẾT THÚC THÀNH CÔNG!</b>\nCảm ơn bạn đã tin tưởng hệ thống.", parse_mode=ParseMode.HTML)

    elif data.startswith("cancel_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade and (user_id == trade['buyer_id'] or username == trade['seller_name'].lower()):
            if trade['status'] == Status.PENDING:
                db.update_trade(code, status=Status.CANCELLED)
                db.add_log(code, "HỦY ĐƠN", f"Bởi: {username}")
                await query.edit_message_caption("❌ Giao dịch đã bị hủy.")
            else: await query.answer("❌ Không thể hủy đơn này!", show_alert=True)

# ==========================================================
#                      RUNNER
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
        
