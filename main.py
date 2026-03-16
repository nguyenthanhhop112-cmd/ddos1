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
    InlineKeyboardMarkup,
    Message
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
    "fee_percent": 0.01,
    "aml_note": "⚠️ <b>CHÍNH SÁCH AML:</b> Nghiêm cấm tiền bẩn/rửa tiền. Hệ thống sẽ đóng băng đơn nếu có dấu hiệu nghi vấn.",
    "spam_delay": 2.0  # Giây giữa các lệnh
}

DB_FILE = "system_pro_v16.sqlite3"
user_cooldowns: Dict[int, float] = {}

class Status:
    PENDING = "⏳ CHỜ THANH TOÁN"
    HOLDING = "🛡 BOT GIỮ TIỀN"
    BUYER_DONE = "📦 KHÁCH NHẬN HÀNG"
    PAYOUT_WAIT = "🏛 CHỜ GIẢI NGÂN"
    COMPLETED = "✅ THÀNH CÔNG"
    CANCELLED = "❌ ĐÃ HỦY"

# ==========================================================
#                      DATABASE PRO ARCHITECTURE
# ==========================================================
class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self.conn:
            # Bảng lưu giao dịch
            self.conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE, group_id INTEGER, group_name TEXT, group_link TEXT,
                buyer_id INTEGER, buyer_name TEXT, buyer_user TEXT,
                seller_name TEXT, amount INTEGER, fee INTEGER, total_pay INTEGER,
                product_name TEXT, seller_bank_info TEXT, status TEXT, 
                qr_msg_id INTEGER, status_msg_id INTEGER, created_at TEXT)''')
            
            # Bảng lưu lịch sử (Logs)
            self.conn.execute('''CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT, action TEXT, user_info TEXT, timestamp TEXT)''')

    def create_trade(self, data):
        with self.conn:
            self.conn.execute("""INSERT INTO trades 
                (code, group_id, group_name, group_link, buyer_id, buyer_name, buyer_user, seller_name, 
                 amount, fee, total_pay, product_name, status, created_at) 
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data['code'], data['group_id'], data['group_name'], data['group_link'], data['buyer_id'], data['buyer_name'],
                 data['buyer_user'], data['seller_name'], data['amount'], data['fee'],
                 data['total_pay'], data['product_name'], Status.PENDING, datetime.now().strftime("%H:%M:%S %d/%m/%Y")))
            self.add_log(data['code'], "TẠO ĐƠN", f"Người mua: {data['buyer_name']}")

    def add_log(self, code, action, user_info):
        with self.conn:
            self.conn.execute("INSERT INTO logs (code, action, user_info, timestamp) VALUES (?,?,?,?)",
                             (code, action, user_info, datetime.now().strftime("%H:%M:%S %d/%m/%Y")))

    def get_trade(self, code):
        return self.conn.execute("SELECT * FROM trades WHERE code = ?", (code,)).fetchone()

    def update_trade(self, code, **kwargs):
        set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [code]
        with self.conn:
            self.conn.execute(f"UPDATE trades SET {set_clause} WHERE code = ?", values)

    def get_stats(self):
        with self.conn:
            return self.conn.execute("""SELECT COUNT(*), SUM(amount), SUM(fee) FROM trades WHERE status = ?""", (Status.COMPLETED,)).fetchone()

db = Database()
app = FastAPI()
tg_app = Application.builder().token(CONFIG["bot_token"]).build()

# ==========================================================
#                      UTILITIES (CHỐNG SPAM & PHÂN TÍCH)
# ==========================================================
def is_spamming(user_id: int) -> bool:
    current_time = time.time()
    last_time = user_cooldowns.get(user_id, 0)
    if current_time - last_time < CONFIG["spam_delay"]:
        return True
    user_cooldowns[user_id] = current_time
    return False

# ==========================================================
#                      WEBHOOK SEPAY (XỬ LÝ TIỀN)
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
            asyncio.create_task(handle_payment_success(code, amount_in))
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

async def handle_payment_success(code, amount):
    trade = db.get_trade(code)
    if not trade or trade['status'] != Status.PENDING: return
    
    if amount >= int(trade['total_pay']):
        db.update_trade(code, status=Status.HOLDING)
        db.add_log(code, "THANH TOÁN", f"Nhận: {amount:,} VND")
        
        # Unpin QR cũ
        try: await tg_app.bot.unpin_chat_message(trade['group_id'], trade['qr_msg_id'])
        except: pass

        msg = f"""<b>✅ ĐÃ NHẬN TIỀN - GIAO DỊCH: {code}</b>
━━━━━━━━━━━━━━━━━━━━
💰 <b>Số tiền:</b> {amount:,} VND
📦 <b>Sản phẩm:</b> {trade['product_name']}
🛡 <b>Trạng thái:</b> BOT ĐANG TẠM GIỮ TIỀN

👤 <b>Người mua:</b> {trade['buyer_name']}
👤 <b>Người bán:</b> {trade['seller_name']}
━━━━━━━━━━━━━━━━━━━━
🚀 <b>HÀNH ĐỘNG:</b> Mời người bán giao hàng. Sau khi nhận đủ, người mua bấm nút bên dưới."""
        
        kb = [[InlineKeyboardButton("✅ TÔI ĐÃ NHẬN HÀNG", callback_data=f"done_{code}")]]
        sent = await tg_app.bot.send_message(trade['group_id'], msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        db.update_trade(code, status_msg_id=sent.message_id)
        try: await tg_app.bot.pin_chat_message(trade['group_id'], sent.message_id)
        except: pass

# ==========================================================
#                      COMMAND HANDLERS
# ==========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_spamming(update.effective_user.id): return
    
    kb = [
        [InlineKeyboardButton("➕ Thêm Bot Vào Nhóm", url=f"https://t.me/{(await context.bot.get_me()).username}?startgroup=true")],
        [InlineKeyboardButton("📜 Hướng Dẫn", callback_data="ui_help"), InlineKeyboardButton("📊 Thống Kê", callback_data="ui_stats")],
        [InlineKeyboardButton("👨‍💻 Admin Support", url=f"https://t.me/{CONFIG['admin_handle'][1:]}")]
    ]
    txt = f"<b>💎 NỀN TẢNG TRUNG GIAN TỰ ĐỘNG V16 PRO</b>\n━━━━━━━━━━━━━━━━━━━━\nAn toàn - Tốc độ - Bảo mật tuyệt đối.\n\n{CONFIG['aml_note']}"
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

async def cmd_taogdtg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private": return
    if is_spamming(update.effective_user.id): return

    try:
        raw_text = update.message.text.replace("/taogdtg", "").strip()
        parts = [p.strip() for p in raw_text.split("|")]
        
        amount = int(re.sub(r"\D", "", parts[0]))
        product = parts[1]
        seller = parts[2] # Người dùng có thể Tag @ hoặc nhập tên
        
        code = f"GD{int(datetime.now().timestamp())}"
        fee = max(CONFIG['fee_min'], int(amount * CONFIG['fee_percent']))
        total = amount + fee
        
        # Lấy link nhóm (nếu có)
        try: group_link = await update.effective_chat.export_invite_link()
        except: group_link = "N/A"

        db.create_trade({
            "code": code, "group_id": update.effective_chat.id, "group_name": update.effective_chat.title,
            "group_link": group_link, "buyer_id": update.effective_user.id, "buyer_name": update.effective_user.full_name,
            "buyer_user": f"@{update.effective_user.username}", "seller_name": seller,
            "amount": amount, "fee": fee, "total_pay": total, "product_name": product
        })

        qr = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={total}&addInfo={code}&accountName={CONFIG['bank_owner'].replace(' ', '%20')}"
        txt = f"""<b>🤝 ĐƠN TRUNG GIAN: {code}</b>
━━━━━━━━━━━━━━━━━━━━
📦 <b>Sản phẩm:</b> {product}
👤 <b>Bán:</b> {seller}
👤 <b>Mua:</b> {update.effective_user.full_name}
━━━━━━━━━━━━━━━━━━━━
💵 <b>Tiền hàng:</b> {amount:,} VND
⚙️ <b>Phí:</b> {fee:,} VND
💳 <b>TỔNG THANH TOÁN:</b> <code>{total:,}</code> VND
📝 <b>Nội dung CK:</b> <code>{code}</code>

{CONFIG['aml_note']}"""

        kb = [[InlineKeyboardButton("🔄 Lấy Lại Mã QR", callback_data=f"getqr_{code}"), InlineKeyboardButton("❌ Hủy Đơn", callback_data=f"cancel_{code}")]]
        msg = await update.message.reply_photo(photo=qr, caption=txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        db.update_trade(code, qr_msg_id=msg.message_id)
        try: await msg.pin()
        except: pass

        # Báo Admin
        admin_txt = f"🔔 <b>ĐƠN MỚI:</b> {code}\n💰 Tiền: {total:,}\n📂 Nhóm: {update.effective_chat.title}\n🔗 Link: {group_link}"
        await context.bot.send_message(CONFIG['admin_id'], admin_txt, parse_mode=ParseMode.HTML)

    except:
        await update.message.reply_text("❌ <b>Sai cấu trúc!</b>\nSử dụng: <code>/taogdtg Tiền | Sản phẩm | @NgườiBán</code>", parse_mode=ParseMode.HTML)

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_spamming(update.effective_user.id): return
    if len(context.args) < 2: 
        return await update.message.reply_text("❌ Cú pháp: <code>/bank [MãGD] [STK Bank Tên]</code>", parse_mode=ParseMode.HTML)

    code, info = context.args[0].upper(), " ".join(context.args[1:])
    trade = db.get_trade(code)
    if not trade: return await update.message.reply_text("❌ Mã GD không tồn tại!")

    # LOGIC: Chỉ người bán (người được Tag lúc tạo đơn) mới được rút
    current_user = f"@{update.effective_user.username}".lower()
    if current_user != trade['seller_name'].lower():
        return await update.message.reply_text(f"⛔ <b>BỊ TỪ CHỐI:</b> Chỉ người bán ({trade['seller_name']}) mới có quyền rút tiền đơn này!", parse_mode=ParseMode.HTML)

    if trade['status'] == Status.BUYER_DONE:
        db.update_trade(code, status=Status.PAYOUT_WAIT, seller_bank_info=info)
        db.add_log(code, "YÊU CẦU RÚT", info)
        
        kb = [[InlineKeyboardButton("✅ XÁC NHẬN ĐÃ CHUYỂN", callback_data=f"adminpayout_{code}")]]
        admin_msg = f"🏛 <b>YÊU CẦU GIẢI NGÂN</b>\n━━━━━━━━━━━━━━━━━━━━\n🆔 Đơn: {code}\n💰 Tiền: {trade['amount']:,} VND\n💳 STK: {info}\n📂 Nhóm: {trade['group_name']}\n🔗 Link: {trade['group_link']}"
        await context.bot.send_message(CONFIG['admin_id'], admin_msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        await update.message.reply_text("✅ <b>Đã gửi yêu cầu!</b> Admin sẽ xử lý trong giây lát.")
    else:
        await update.message.reply_text(f"❌ Trạng thái đơn không hợp lệ: {trade['status']}")

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return
    code = context.args[0].upper()
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        logs = conn.execute("SELECT * FROM logs WHERE code = ? ORDER BY id DESC", (code,)).fetchall()
    
    if not logs: return await update.message.reply_text("❌ Không có lịch sử cho đơn này.")
    
    txt = f"📜 <b>LỊCH SỬ ĐƠN {code}:</b>\n"
    for l in logs:
        txt += f"• [{l['timestamp']}] {l['action']}: {l['user_info']}\n"
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

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
            db.add_log(code, "XÁC NHẬN", "Người mua đã nhận hàng")
            
            txt = f"<b>📦 GIAO DỊCH {code} HOÀN TẤT</b>\n\nNgười bán {trade['seller_name']} rút tiền bằng cách gõ:\n<code>/bank {code} [STK Bank Tên]</code>"
            if query.message.photo: await query.edit_message_caption(caption=txt, parse_mode=ParseMode.HTML)
            else: await query.edit_message_text(text=txt, parse_mode=ParseMode.HTML)
        else:
            await query.answer("⛔ Bạn không có quyền xác nhận!", show_alert=True)

    elif data.startswith("adminpayout_"):
        if user_id != CONFIG['admin_id']: return
        code = data.split("_")[1]
        trade = db.get_trade(code)
        db.update_trade(code, status=Status.COMPLETED)
        db.add_log(code, "GIẢI NGÂN", "Admin đã chuyển tiền")
        
        await query.edit_message_text(f"✅ Đã giải ngân thành công đơn {code}")
        await context.bot.send_message(trade['group_id'], f"<b>🎉 GIAO DỊCH {code} KẾT THÚC!</b>\nCảm ơn các bạn đã tin dùng dịch vụ trung gian.", parse_mode=ParseMode.HTML)

    elif data.startswith("cancel_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade and (user_id == trade['buyer_id'] or username == trade['seller_name'].lower()):
            if trade['status'] == Status.PENDING:
                db.update_trade(code, status=Status.CANCELLED)
                db.add_log(code, "HỦY ĐƠN", f"Thực hiện bởi: {username}")
                await query.edit_message_caption("❌ <b>GIAO DỊCH ĐÃ HỦY</b>", parse_mode=ParseMode.HTML)
            else: await query.answer("❌ Không thể hủy đơn lúc này!", show_alert=True)

# ==========================================================
#                      RUNNER CONFIG
# ==========================================================
async def main_runner():
    # Thêm các Handler
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("taogdtg", cmd_taogdtg))
    tg_app.add_handler(CommandHandler("bank", cmd_bank))
    tg_app.add_handler(CommandHandler("history", cmd_history))
    tg_app.add_handler(CallbackQueryHandler(callback_handler))
    
    await tg_app.initialize()
    await tg_app.start()
    asyncio.create_task(tg_app.updater.start_polling())
    
    # Server Webhook
    port = int(os.environ.get("PORT", 10000))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio"))
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main_runner())
        
