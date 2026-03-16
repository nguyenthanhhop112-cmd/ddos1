import os
import re
import sqlite3
import logging
import asyncio
import time
from datetime import datetime
from typing import Optional, Dict

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
    "aml_note": "⚠️ <b>LƯU Ý:</b> Hệ thống nghiêm cấm hành vi rửa tiền. Mọi nguồn tiền bẩn, tiền vi phạm pháp luật nếu bị phát hiện sẽ bị phong tỏa vĩnh viễn và cung cấp thông tin cho cơ quan chức năng.",
    "spam_delay": 2.0  # Chống spam: 2 giây/lệnh
}

DB_FILE = "system_v16.sqlite3"
user_cooldowns: Dict[int, float] = {}

class Status:
    PENDING = "CHO_THANH_TOAN"
    HOLDING = "BOT_DANG_GIU_TIEN"
    BUYER_DONE = "NGUOI_MUA_XAC_NHAN"
    PAYOUT_WAIT = "CHO_GIAI_NGAN"
    COMPLETED = "THANH_CONG"
    CANCELLED = "DA_HUY"

# ==========================================================
#                      HÀM TÍNH PHÍ (FEE LOGIC)
# ==========================================================
def calculate_fee(amount: int) -> int:
    """Tính phí theo đúng yêu cầu của Admin"""
    if amount < 100000:
        return 5000
    elif amount < 500000:  # 100k - 499k
        return 10000
    elif amount < 1000000: # 500k - 999k
        return 15000
    elif amount <= 2000000: # 1tr - 2tr
        return 20000
    else: # Trên 2tr
        return 30000

def check_spam(user_id: int) -> bool:
    """Hàm chống spam cơ bản"""
    now = time.time()
    last = user_cooldowns.get(user_id, 0)
    if now - last < CONFIG["spam_delay"]:
        return True
    user_cooldowns[user_id] = now
    return False

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
            # Bảng giao dịch chính (Thêm group_link)
            self.conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE, group_id INTEGER, group_name TEXT, group_link TEXT,
                buyer_id INTEGER, buyer_name TEXT, buyer_user TEXT,
                seller_name TEXT, amount INTEGER, fee INTEGER, total_pay INTEGER,
                product_name TEXT, seller_bank_info TEXT, status TEXT, 
                qr_msg_id INTEGER, status_msg_id INTEGER, created_at TEXT)''')
            
            # Bảng Logs lưu lịch sử
            self.conn.execute('''CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT, action TEXT, detail TEXT, created_at TEXT)''')

    def create_trade(self, data):
        with self.conn:
            self.conn.execute("""INSERT INTO trades 
                (code, group_id, group_name, group_link, buyer_id, buyer_name, buyer_user, seller_name, 
                 amount, fee, total_pay, product_name, status, created_at) 
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data['code'], data['group_id'], data['group_name'], data['group_link'], data['buyer_id'], data['buyer_name'],
                 data['buyer_user'], data['seller_name'], data['amount'], data['fee'],
                 data['total_pay'], data['product_name'], Status.PENDING, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    def log_action(self, code, action, detail):
        with self.conn:
            self.conn.execute("INSERT INTO logs (code, action, detail, created_at) VALUES (?,?,?,?)",
                             (code, action, detail, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

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
        db.log_action(code, "NHẬN TIỀN", f"Đã nhận {amount_received:,} VND qua Webhook")
        
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
    if check_spam(update.effective_user.id): return
    bot_info = await context.bot.get_me()
    keyboard = [
        [InlineKeyboardButton("➕ Thêm Bot Vào Nhóm", url=f"https://t.me/{bot_info.username}?startgroup=true")],
        [InlineKeyboardButton("📖 Hướng Dẫn & Biểu Phí", callback_data="ui_help")],
        [InlineKeyboardButton("📊 Thống Kê Giao Dịch", callback_data="ui_stats"), InlineKeyboardButton("👨‍💻 Admin", url=f"https://t.me/{CONFIG['admin_handle'][1:]}")]
    ]
    
    txt = f"""<b>🌟 HỆ THỐNG TRUNG GIAN TỰ ĐỘNG 🌟</b>
━━━━━━━━━━━━━━━━━━━━
Chào mừng bạn đến với nền tảng Giao Dịch An Toàn, Nhanh Chóng và Tự Động 100%.

<b>🛡 ƯU ĐIỂM VƯỢT TRỘI:</b>
✅ Bot giữ tiền trung gian minh bạch, chống lừa đảo (SCAM).
✅ Nạp rút duyệt tự động qua ngân hàng chỉ trong 3 giây.
✅ Có lưu lịch sử đối soát rõ ràng cho từng giao dịch.

<b>💰 BIỂU PHÍ HỆ THỐNG (Người mua chịu):</b>
• Dưới 100k: Phí <b>5k</b>
• Từ 100k – 499k: Phí <b>10k</b>
• Từ 500k – 999k: Phí <b>15k</b>
• Từ 1tr – 2tr: Phí <b>20k</b>
• Trên 2tr: Phí <b>30k</b>

📌 <i>Gõ <code>/taogdtg</code> trong nhóm để bắt đầu giao dịch! Bấm nút <b>[Hướng Dẫn]</b> bên dưới để xem quy trình.</i>

{CONFIG['aml_note']}"""
    
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def cmd_taogdtg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return await update.message.reply_text("❌ Lệnh này chỉ dùng trong Nhóm Giao Dịch!")
    if check_spam(update.effective_user.id): return
    
    try:
        parts = [p.strip() for p in update.message.text.replace("/taogdtg", "").split("|")]
        if len(parts) < 3: raise ValueError
        
        amount = int(re.sub(r"\D", "", parts[0]))
        product = parts[1]
        seller = parts[2] # Có thể là @username hoặc tên
        
        if amount < 1000:
            return await update.message.reply_text("❌ Số tiền tối thiểu là 1,000 VND!")

        code = f"GD{int(datetime.now().timestamp())}"
        
        fee = calculate_fee(amount)
        total = amount + fee

        try: group_link = await update.effective_chat.export_invite_link()
        except: group_link = "Chưa cấp quyền Admin cho Bot"

        db.create_trade({
            "code": code, "group_id": update.effective_chat.id, "group_name": update.effective_chat.title, "group_link": group_link,
            "buyer_id": update.effective_user.id, "buyer_name": update.effective_user.full_name,
            "buyer_user": f"@{update.effective_user.username}", "seller_name": seller,
            "amount": amount, "fee": fee, "total_pay": total, "product_name": product
        })
        db.log_action(code, "TẠO ĐƠN", f"Người mua: {update.effective_user.full_name} | Sản phẩm: {product}")

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
📝 <b>Nội dung chuyển khoản:</b> <code>{code}</code>

{CONFIG['aml_note']}"""

        kb = [[InlineKeyboardButton("🔄 Lấy Lại Mã QR", callback_data=f"getqr_{code}"), InlineKeyboardButton("❌ Hủy Đơn", callback_data=f"cancel_{code}")]]
        msg = await update.message.reply_photo(photo=qr, caption=txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        db.update_trade(code, qr_msg_id=msg.message_id)
        try: await msg.pin() 
        except: pass
    except Exception as e:
        logger.error(f"Lỗi tạo đơn: {e}")
        await update.message.reply_text("❌ <b>Sai cú pháp!</b>\nSử dụng: <code>/taogdtg Tiền | Sản phẩm | @Seller</code>\nVí dụ: <code>/taogdtg 100000 | Nick Game | @admin</code>", parse_mode=ParseMode.HTML)

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if check_spam(update.effective_user.id): return
    if len(context.args) < 2: 
        return await update.message.reply_text("❌ Cú pháp: <code>/bank [MãGD] [STK Bank Tên]</code>\nVí dụ: <code>/bank GD12345 0123456789 MB Bank Nguyen Van A</code>", parse_mode=ParseMode.HTML)
    
    code, info = context.args[0].upper(), " ".join(context.args[1:])
    trade = db.get_trade(code)
    
    if not trade:
        return await update.message.reply_text("❌ Không tìm thấy mã giao dịch này!")

    curr_user = f"@{update.effective_user.username}"
    if curr_user.lower() != trade['seller_name'].lower():
        return await update.message.reply_text(f"⛔ Quyền hạn: Chỉ người bán (<b>{trade['seller_name']}</b>) mới có quyền rút tiền đơn này!", parse_mode=ParseMode.HTML)

    if trade['status'] == Status.BUYER_DONE:
        db.update_trade(code, status=Status.PAYOUT_WAIT, seller_bank_info=info)
        db.log_action(code, "YÊU CẦU RÚT", f"Bank: {info}")
        kb = [[InlineKeyboardButton("✅ XÁC NHẬN ĐÃ GIẢI NGÂN", callback_data=f"adminpayout_{code}")]]
        
        admin_txt = f"""🏛 <b>YÊU CẦU RÚT TIỀN: {code}</b>
━━━━━━━━━━━━━━━━━━━━
💰 <b>Số tiền cần chuyển:</b> {trade['amount']:,} VND
💳 <b>Thông tin Bank:</b> <code>{info}</code>
👥 <b>Người Bán:</b> {trade['seller_name']}
📂 <b>Nhóm:</b> {trade['group_name']}
🔗 <b>Link Nhóm:</b> {trade['group_link']}"""
        
        await context.bot.send_message(CONFIG['admin_id'], admin_txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        await update.message.reply_text("✅ <b>Yêu cầu rút tiền thành công!</b>\nAdmin đã nhận thông báo và sẽ giải ngân cho bạn ngay.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"❌ Trạng thái đơn không hợp lệ để rút tiền! (Trạng thái hiện tại: {trade['status']})")

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if check_spam(update.effective_user.id): return
    if not context.args: return await update.message.reply_text("❌ Vui lòng nhập mã đơn!\nCú pháp: <code>/check [MãGD]</code>", parse_mode=ParseMode.HTML)
    code = context.args[0].upper()
    trade = db.get_trade(code)
    if not trade: return await update.message.reply_text("❌ Đơn không tồn tại!")
    
    txt = f"""<b>🔍 THÔNG TIN GIAO DỊCH: {code}</b>
━━━━━━━━━━━━━━━━━━━━
📦 <b>Sản phẩm:</b> {trade['product_name']}
💵 <b>Số tiền hàng:</b> {trade['amount']:,} VND
⚙️ <b>Phí giao dịch:</b> {trade['fee']:,} VND
💳 <b>Tổng thanh toán:</b> {trade['total_pay']:,} VND
🛡 <b>Trạng thái:</b> <code>{trade['status']}</code>
⏰ <b>Thời gian tạo:</b> {trade['created_at']}
👤 <b>Người bán:</b> {trade['seller_name']}
👤 <b>Người mua:</b> {trade['buyer_name']}"""
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def cmd_huy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if check_spam(update.effective_user.id): return
    if not context.args: return await update.message.reply_text("❌ Vui lòng nhập mã đơn cần hủy!\nCú pháp: <code>/huy [MãGD]</code>", parse_mode=ParseMode.HTML)
    code = context.args[0].upper()
    trade = db.get_trade(code)
    if not trade: return await update.message.reply_text("❌ Đơn không tồn tại!")
    
    user_id = update.effective_user.id
    curr_user = f"@{update.effective_user.username}".lower()
    
    if user_id == trade['buyer_id'] or curr_user == trade['seller_name'].lower():
        if trade['status'] == Status.PENDING:
            db.update_trade(code, status=Status.CANCELLED)
            db.log_action(code, "HỦY ĐƠN", f"Bởi user_id: {user_id}")
            await update.message.reply_text(f"✅ Đã hủy giao dịch {code} thành công!")
        else:
            await update.message.reply_text("❌ Chỉ có thể hủy khi đơn đang ở trạng thái CHỜ THANH TOÁN!")
    else:
        await update.message.reply_text("⛔ Bạn không có quyền hủy đơn này!")

async def cmd_thongke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != CONFIG['admin_id']: return
    s = db.get_stats()
    txt = f"""<b>📊 THỐNG KÊ DOANH THU HỆ THỐNG</b>
━━━━━━━━━━━━━━━━━━━━
✅ <b>Tổng đơn thành công:</b> {s['total_count'] or 0} đơn
💰 <b>Tổng tiền hàng đã xử lý:</b> {s['total_amount'] or 0:,} VND
💎 <b>Tổng lợi nhuận (Phí thu được):</b> {s['total_fee'] or 0:,} VND"""
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    username = f"@{update.effective_user.username}"

    if data == "ui_help":
        txt = """<b>📖 HƯỚNG DẪN QUY TRÌNH GIAO DỊCH</b>
━━━━━━━━━━━━━━━━━━━━
1️⃣ <b>Tạo đơn:</b> Trong nhóm gõ lệnh:
👉 <code>/taogdtg Tiền | Sản phẩm | @UsernameNgườiBán</code>

2️⃣ <b>Thanh toán:</b> Người mua Quét mã QR chuyển tiền cho Bot kèm nội dung Mã GD.

3️⃣ <b>Giao hàng:</b> Bot báo ĐÃ NHẬN TIỀN -> Người bán tiến hành giao hàng.

4️⃣ <b>Xác nhận:</b> Người mua nhận hàng xong bấm nút <b>[✅ TÔI ĐÃ NHẬN ĐỦ HÀNG]</b>.

5️⃣ <b>Rút tiền:</b> Người bán dùng <code>/bank</code> để yêu cầu giải ngân."""
        kb = [[InlineKeyboardButton("🔙 Quay Lại Menu Chính", callback_data="ui_back")]]
        await query.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

    elif data == "ui_stats":
        s = db.get_stats()
        txt = f"<b>📊 THỐNG KÊ CHUNG HỆ THỐNG</b>\n━━━━━━━━━━━━━━━━━━━━\n✅ Tổng số giao dịch thành công: {s['total_count'] or 0} đơn"
        kb = [[InlineKeyboardButton("🔙 Quay Lại Menu Chính", callback_data="ui_back")]]
        await query.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

    elif data == "ui_back":
        await cmd_start(update, context)

    elif data.startswith("getqr_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade:
            qr = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={trade['total_pay']}&addInfo={code}&accountName={CONFIG['bank_owner'].replace(' ', '%20')}"
            await query.message.reply_photo(photo=qr, caption=f"🔄 Mã QR thanh toán của đơn <b>{code}</b>", parse_mode=ParseMode.HTML)
            await query.answer()

    elif data.startswith("cancel_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade and (user_id == trade['buyer_id'] or username.lower() == trade['seller_name'].lower()):
            if trade['status'] == Status.PENDING:
                db.update_trade(code, status=Status.CANCELLED)
                db.log_action(code, "HỦY ĐƠN", f"Hủy qua nút bởi {username}")
                if query.message.photo: await query.edit_message_caption("❌ <b>GIAO DỊCH NÀY ĐÃ ĐƯỢC HỦY</b>", parse_mode=ParseMode.HTML)
                else: await query.edit_message_text("❌ <b>GIAO DỊCH NÀY ĐÃ ĐƯỢC HỦY</b>", parse_mode=ParseMode.HTML)
            else: await query.answer("❌ Không thể hủy đơn lúc này!", show_alert=True)
        else: await query.answer("⛔ Bạn không có quyền!", show_alert=True)

    elif data.startswith("done_"):
        code = dat
