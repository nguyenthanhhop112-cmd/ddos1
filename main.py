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
    "fee_percent": 0.01,
    "aml_note": "⚠️ <b>LƯU Ý:</b> Hệ thống nghiêm cấm hành vi rửa tiền. Mọi nguồn tiền bẩn, tiền vi phạm pháp luật nếu bị phát hiện sẽ bị phong tỏa vĩnh viễn và cung cấp thông tin cho cơ quan chức năng."
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
            # Thêm các cột quản lý hủy đơn và ghi chú
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
    
    def get_stats(self):
        with self.conn:
            res = self.conn.execute("""SELECT 
                COUNT(*) as total_count, 
                SUM(amount) as total_amount, 
                SUM(fee) as total_fee 
                FROM trades WHERE status = ?""", (Status.COMPLETED,)).fetchone()
            return res

db = Database()
app = FastAPI()
tg_app = Application.builder().token(CONFIG["bot_token"]).build()

# ==========================================================
#                      WEBHOOK SEPAY (NHẬN BILL)
# ==========================================================
@app.get("/")
async def health_check():
    """Endpoint giúp treo bot trên Render không bị sleep"""
    return {"status": "online", "timestamp": datetime.now().isoformat()}

@app.post("/webhook")
async def sepay_webhook(request: Request):
    try:
        data = await request.json()
        logger.info(f"📩 Webhook Incoming: {data}")
        content = str(data.get("content", "")).upper()
        
        raw_val = data.get("amount_in") or data.get("amount") or data.get("transferAmount") or "0"
        clean_val = re.sub(r"\D", "", str(raw_val))
        amount_in = int(clean_val) if clean_val else 0
        
        match = re.search(r"GD(\d+)", content)
        if match:
            code = f"GD{match.group(1)}"
            logger.info(f"🔔 BILL NHẬN: {code} | Số tiền: {amount_in}")
            asyncio.create_task(process_paid_invoice(code, amount_in))
        
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Lỗi Webhook: {e}")
        return {"status": "error"}

async def process_paid_invoice(code, amount_received):
    trade = db.get_trade(code)
    if not trade or trade['status'] != Status.PENDING:
        return

    total_needed = int(trade['total_pay'])
    if int(amount_received) >= total_needed:
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
🚀 <b>YÊU CẦU:</b> Người bán tiến hành giao hàng. Sau khi xong, người mua bấm nút xác nhận dưới đây.
{CONFIG['aml_note']}"""
        
        btn = [[InlineKeyboardButton("✅ TÔI ĐÃ NHẬN ĐỦ HÀNG", callback_data=f"done_{code}")]]
        sent = await tg_app.bot.send_message(chat_id=trade['group_id'], text=msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(btn))
        db.update_trade(code, status_msg_id=sent.message_id)
        try: await tg_app.bot.pin_chat_message(chat_id=trade['group_id'], message_id=sent.message_id)
        except: pass
    else:
        missing = total_needed - amount_received
        txt = f"""<b>⚠️ CẢNH BÁO: CHUYỂN THIẾU TIỀN</b>
━━━━━━━━━━━━━━━━━━━━
🆔 <b>Mã đơn:</b> <code>{code}</code>
💰 <b>Cần thanh toán:</b> {total_needed:,} VND
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
        [InlineKeyboardButton("➕ Thêm Bot Vào Nhóm", url=f"https://t.me/{bot_info.username}?startgroup=true")],
        [InlineKeyboardButton("📖 Hướng Dẫn", callback_data="ui_help"), InlineKeyboardButton("📊 Thống Kê", callback_data="ui_stats")],
        [InlineKeyboardButton("👨‍💻 Liên Hệ Admin", url=f"https://t.me/{CONFIG['admin_handle'][1:]}")]
    ]
    
    txt = f"""<b>⚡ HỆ THỐNG TRUNG GIAN TỰ ĐỘNG 4.0</b>
━━━━━━━━━━━━━━━━━━━━
Chào mừng bạn đến với nền tảng Giao Dịch an toàn.

<b>💎 TÍNH NĂNG:</b>
• 🛡 <b>An Toàn:</b> Bot giữ tiền trung gian minh bạch.
• ⚡ <b>Tốc Độ:</b> Xác thực Bank tự động 100%.
• 🚫 <b>Phòng Chống:</b> Hệ thống quét tiền bẩn & lừa đảo.

{CONFIG['aml_note']}"""
    
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def cmd_taogdtg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return await update.message.reply_text("❌ Lệnh này chỉ dùng trong Nhóm Giao Dịch!")
    
    try:
        parts = [p.strip() for p in update.message.text.replace("/taogdtg", "").split("|")]
        if len(parts) < 3: raise ValueError
        
        amount = int(re.sub(r"\D", "", parts[0]))
        product = parts[1]
        seller = parts[2] 
        
        if amount < 1000:
            return await update.message.reply_text("❌ Số tiền tối thiểu là 1,000 VND!")

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
        txt = f"""<b>🤝 ĐƠN GIAO DỊCH MỚI: {code}</b>
━━━━━━━━━━━━━━━━━━━━
📦 <b>Sản phẩm:</b> {product}
👤 <b>Người Bán:</b> {seller}
👤 <b>Người Mua:</b> {update.effective_user.full_name}
━━━━━━━━━━━━━━━━━━━━
💵 <b>Tiền hàng:</b> {amount:,} VND
⚙️ <b>Phí GD:</b> {fee:,} VND
💳 <b>TỔNG THANH TOÁN:</b> <code>{total:,}</code> VND
📝 <b>Nội dung:</b> <code>{code}</code>

{CONFIG['aml_note']}"""

        kb = [[InlineKeyboardButton("🔄 Lấy Lại Mã QR", callback_data=f"getqr_{code}"), InlineKeyboardButton("❌ Hủy Đơn", callback_data=f"cancel_{code}")]]
        msg = await update.message.reply_photo(photo=qr, caption=txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        db.update_trade(code, qr_msg_id=msg.message_id)
        try: await msg.pin() 
        except: pass
    except:
        await update.message.reply_text("❌ <b>Sai cú pháp!</b>\nSử dụng: <code>/taogdtg Tiền | Sản phẩm | @Seller</code>", parse_mode=ParseMode.HTML)

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2: 
        return await update.message.reply_text("❌ Cú pháp: <code>/bank [MãGD] [STK Bank Tên]</code>", parse_mode=ParseMode.HTML)
    
    code, info = context.args[0].upper(), " ".join(context.args[1:])
    trade = db.get_trade(code)
    
    if not trade:
        return await update.message.reply_text("❌ Không tìm thấy mã giao dịch này!")

    curr_user = f"@{update.effective_user.username}"
    if curr_user.lower() != trade['seller_name'].lower():
        return await update.message.reply_text(f"⛔ Quyền hạn: Chỉ người bán (<b>{trade['seller_name']}</b>) mới có quyền rút tiền đơn này!", parse_mode=ParseMode.HTML)

    if trade['status'] == Status.BUYER_DONE:
        db.update_trade(code, status=Status.PAYOUT_WAIT, seller_bank_info=info)
        kb = [[InlineKeyboardButton("✅ XÁC NHẬN ĐÃ BANK", callback_data=f"adminpayout_{code}")]]
        await context.bot.send_message(CONFIG['admin_id'], f"🏛 <b>YÊU CẦU RÚT TIỀN: {code}</b>\n💰 Tiền: {trade['amount']:,} VND\n💳 STK: {info}\n👥 Seller: {trade['seller_name']}\n📂 Nhóm: {trade['group_name']}", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        await update.message.reply_text("✅ <b>Yêu cầu thành công!</b>\nAdmin đang thực hiện chuyển khoản cho bạn.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"❌ Trạng thái đơn không hợp lệ để rút tiền! (Hiện tại: {trade['status']})")

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("❌ Vui lòng nhập mã đơn!")
    code = context.args[0].upper()
    trade = db.get_trade(code)
    if not trade: return await update.message.reply_text("❌ Đơn không tồn tại!")
    
    txt = f"""<b>🔍 THÔNG TIN ĐƠN: {code}</b>
━━━━━━━━━━━━━━━━━━━━
📦 <b>SP:</b> {trade['product_name']}
💵 <b>Số tiền:</b> {trade['amount']:,} VND
🛡 <b>Trạng thái:</b> <code>{trade['status']}</code>
⏰ <b>Ngày tạo:</b> {trade['created_at']}
👤 <b>Bán:</b> {trade['seller_name']}
👤 <b>Mua:</b> {trade['buyer_name']}"""
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def cmd_huy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("❌ Vui lòng nhập mã đơn cần hủy!")
    code = context.args[0].upper()
    trade = db.get_trade(code)
    if not trade: return await update.message.reply_text("❌ Đơn không tồn tại!")
    
    user_id = update.effective_user.id
    curr_user = f"@{update.effective_user.username}".lower()
    
    if user_id == trade['buyer_id'] or curr_user == trade['seller_name'].lower():
        if trade['status'] == Status.PENDING:
            db.update_trade(code, status=Status.CANCELLED)
            await update.message.reply_text(f"✅ Đã hủy giao dịch {code} thành công!")
        else:
            await update.message.reply_text("❌ Chỉ có thể hủy khi đơn đang ở trạng thái Chờ Thanh Toán!")
    else:
        await update.message.reply_text("⛔ Bạn không có quyền hủy đơn này!")

async def cmd_thongke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != CONFIG['admin_id']: return
    s = db.get_stats()
    txt = f"""<b>📊 THỐNG KÊ HỆ THỐNG</b>
━━━━━━━━━━━━━━━━━━━━
✅ <b>Đơn thành công:</b> {s['total_count'] or 0} đơn
💰 <b>Tổng tiền hàng:</b> {s['total_amount'] or 0:,} VND
💎 <b>Phí thu được:</b> {s['total_fee'] or 0:,} VND"""
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    username = f"@{update.effective_user.username}"

    if data == "ui_help":
        txt = """<b>📖 HƯỚNG DẪN QUY TRÌNH GD</b>
━━━━━━━━━━━━━━━━━━━━
1️⃣ <b>Tạo đơn:</b> Dùng <code>/taogdtg Tiền | SP | @Seller</code>
2️⃣ <b>Thanh toán:</b> Người mua Quét mã QR chuyển tiền cho Bot.
3️⃣ <b>Giao hàng:</b> Bot nhận tiền -> Báo người bán giao hàng.
4️⃣ <b>Xác nhận:</b> Người mua nhận xong bấm <b>[Đã nhận hàng]</b>.
5️⃣ <b>Rút tiền:</b> Người bán dùng <code>/bank</code> để nhận tiền về STK."""
        await query.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Quay Lại", callback_data="ui_back")]]))

    elif data == "ui_back":
        await cmd_start(update, context)

    elif data.startswith("getqr_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade:
            qr = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={trade['total_pay']}&addInfo={code}&accountName={CONFIG['bank_owner'].replace(' ', '%20')}"
            await query.message.reply_photo(photo=qr, caption=f"🔄 Mã QR của đơn <b>{code}</b>", parse_mode=ParseMode.HTML)
            await query.answer()

    elif data.startswith("cancel_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade and (user_id == trade['buyer_id'] or username.lower() == trade['seller_name'].lower()):
            if trade['status'] == Status.PENDING:
                db.update_trade(code, status=Status.CANCELLED)
                await query.edit_message_caption("❌ Giao dịch này đã được hủy bởi người trong cuộc.")
            else: await query.answer("❌ Không thể hủy đơn này!", show_alert=True)
        else: await query.answer("⛔ Bạn không có quyền!", show_alert=True)

    elif data.startswith("done_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if not trade: return await query.answer("❌ Đơn không tồn tại!")
        if user_id != trade['buyer_id']:
            return await query.answer("⛔ Chỉ người mua mới được xác nhận nhận hàng!", show_alert=True)
        
        if trade['status'] == Status.HOLDING:
            db.update_trade(code, status=Status.BUYER_DONE)
            await query.answer("✅ Đã xác nhận! Chờ người bán rút tiền.", show_alert=True)
            txt = f"<b>📦 GIAO DỊCH {code} HOÀN TẤT</b>\n\nNgười bán {trade['seller_name']} vui lòng rút tiền bằng cú pháp:\n<code>/bank {code} [STK Bank Tên]</code>"
            if query.message.photo: await query.edit_message_caption(caption=txt, parse_mode=ParseMode.HTML)
            else: await query.edit_message_text(text=txt, parse_mode=ParseMode.HTML)
        else:
            await query.answer("⚠️ Trạng thái đơn không hợp lệ!", show_alert=True)

    elif data.startswith("adminpayout_"):
        if user_id != CONFIG['admin_id']: return
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade['status'] == Status.PAYOUT_WAIT:
            db.update_trade(code, status=Status.COMPLETED)
            await query.edit_message_text(f"✅ Đã giải ngân thành công đơn {code}")
            await context.bot.send_message(trade['group_id'], f"<b>🎉 CHÚC MỪNG! GIAO DỊCH {code} ĐÃ HOÀN TẤT 100%</b>\nCảm ơn các bạn đã sử dụng dịch vụ trung gian uy tín.", parse_mode=ParseMode.HTML)
        else:
            await query.answer("⚠️ Đơn này đã được xử lý trước đó.")

# ==========================================================
#                      RUNNER
# ==========================================================
async def main_runner():
    # Đăng ký các Handler
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("taogdtg", cmd_taogdtg))
    tg_app.add_handler(CommandHandler("bank", cmd_bank))
    tg_app.add_handler(CommandHandler("check", cmd_check))
    tg_app.add_handler(CommandHandler("huy", cmd_huy))
    tg_app.add_handler(CommandHandler("thongke", cmd_thongke))
    tg_app.add_handler(CallbackQueryHandler(callback_handler))
    
    # Khởi tạo bot
    await tg_app.initialize()
    await tg_app.start()
    
    # Chạy Polling cho Telegram trong background
    asyncio.create_task(tg_app.updater.start_polling())
    logger.info("🤖 Bot Telegram is running...")
    
    # Chạy Webhook Server (FastAPI)
    # Cấu hình port cho Render hoặc Replit
    port = int(os.environ.get("PORT", 8080)) 
    config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    
    logger.info(f"🌐 Webhook Server running on port {port}")
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(main_runner())
    except KeyboardInterrupt:
        pass
