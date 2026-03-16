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
# Cấu hình Logging để bạn xem lỗi trên Render dễ hơn
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

CONFIG = {
    "bot_token": "8560020347:AAECTuhAhuIvYz2pvDmwXS9mK4nEN-g-0EM",
    "admin_id": 7816353760,
    "admin_handle": "@nth_dev", 
    "bank_name": "MSB",
    "bank_bin": "970426",
    "bank_stk": "96886693002613",
    "bank_owner": "NGUYEN THANH HOP",
    "aml_note": "⚠️ <b>CHÍNH SÁCH:</b> Nghiêm cấm tiền bẩn. Đơn hàng nghi vấn sẽ bị đóng băng vĩnh viễn.",
    "spam_delay": 2.0,
    "maintenance": False  # Chế độ bảo trì (mặc định tắt)
}

DB_FILE = "system_v16_pro_ultimate.sqlite3"
user_cooldowns: Dict[int, float] = {}

class Status:
    PENDING = "⏳ CHỜ THANH TOÁN"
    HOLDING = "🛡 BOT GIỮ TIỀN"
    BUYER_DONE = "📦 KHÁCH NHẬN HÀNG"
    PAYOUT_WAIT = "🏛 CHỜ GIẢI NGÂN"
    COMPLETED = "✅ THÀNH CÔNG"
    CANCELLED = "❌ ĐÃ HỦY"
    DISPUTED = "🆘 ĐANG KHIẾU NẠI"

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
            # Bảng giao dịch chính
            self.conn.execute('''CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE, 
                group_id INTEGER, 
                group_name TEXT, 
                group_link TEXT,
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
                created_at TEXT, 
                completed_at TEXT)''')
            
            # Bảng lưu uy tín người dùng
            self.conn.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                reputation INTEGER DEFAULT 0)''')
            
            # Bảng Log hệ thống
            self.conn.execute('''CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT, 
                action TEXT, 
                detail TEXT, 
                timestamp TEXT)''')

    def create_trade(self, data):
        with self.conn:
            self.conn.execute("""INSERT INTO trades 
                (code, group_id, group_name, group_link, buyer_id, buyer_name, buyer_user, seller_name, 
                 amount, fee, total_pay, product_name, status, created_at) 
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data['code'], data['group_id'], data['group_name'], data['group_link'], data['buyer_id'], data['buyer_name'],
                 data['buyer_user'], data['seller_name'], data['amount'], data['fee'],
                 data['total_pay'], data['product_name'], Status.PENDING, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            self.add_log(data['code'], "TẠO ĐƠN", f"Mua: {data['buyer_name']} | Bán: {data['seller_name']}")

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

    def update_reputation(self, user_id, username):
        with self.conn:
            self.conn.execute("""INSERT INTO users (user_id, username, reputation) 
                VALUES (?, ?, 1) ON CONFLICT(user_id) DO UPDATE SET reputation = reputation + 1""", 
                (user_id, username))

    def get_reputation(self, user_id):
        res = self.conn.execute("SELECT reputation FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return res[0] if res else 0

db = Database()
app = FastAPI()
tg_app = Application.builder().token(CONFIG["bot_token"]).build()

# ==========================================================
#                      HÀM TIỆN ÍCH
# ==========================================================
def calculate_fee(amount: int) -> int:
    if amount < 100000: return 5000
    if amount < 1000000: return 10000
    if amount < 5000000: return 30000
    return 50000

async def send_auto_backup(context: ContextTypes.DEFAULT_TYPE):
    """Gửi file database về cho Admin để tránh mất dữ liệu trên Render"""
    try:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, 'rb') as f:
                await context.bot.send_document(
                    chat_id=CONFIG["admin_id"], 
                    document=f, 
                    caption=f"📂 <b>BACKUP DATABASE</b>\n⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
                    parse_mode=ParseMode.HTML
                )
    except Exception as e:
        logger.error(f"Lỗi Backup: {e}")

# ==========================================================
#                      WEBHOOK NHẬN TIỀN (FIXED)
# ==========================================================
@app.post("/webhook")
async def sepay_webhook(request: Request):
    try:
        data = await request.json()
        logger.info(f"📩 Dữ liệu Webhook: {data}")
        
        content = str(data.get("content", "")).upper()
        # Xử lý số tiền (chấp nhận cả số và chuỗi)
        raw_val = data.get("amount_in") or data.get("amount") or "0"
        amount_in = int(float(re.sub(r"[^\d.]", "", str(raw_val))))
        
        # Tìm mã GD trong nội dung chuyển khoản
        match = re.search(r"GD\s?(\d+)", content)
        if match:
            code = f"GD{match.group(1)}"
            asyncio.create_task(process_paid(code, amount_in))
            return {"status": "success", "msg": "Mã đơn hợp lệ"}
        
        return {"status": "ignored", "msg": "Không tìm thấy mã GD"}
    except Exception as e:
        logger.error(f"❌ Lỗi xử lý Webhook: {e}")
        return {"status": "error", "msg": str(e)}

async def process_paid(code, amount_received):
    try:
        trade = db.get_trade(code)
        if not trade:
            logger.warning(f"⚠️ Nhận tiền cho mã đơn {code} nhưng không có trong DB")
            return

        if trade['status'] != Status.PENDING:
            logger.info(f"ℹ️ Đơn {code} đã xử lý trước đó.")
            return

        total_needed = int(trade['total_pay'])
        
        # Nếu nhận đủ hoặc dư tiền
        if amount_received >= total_needed:
            db.update_trade(code, status=Status.HOLDING)
            db.add_log(code, "THANH TOÁN", f"Nhận đủ {amount_received:,}đ")

            # 1. Dọn dẹp tin nhắn QR cũ
            try: await tg_app.bot.delete_message(trade['group_id'], trade['qr_msg_id'])
            except: pass

            # 2. Thông báo vào nhóm (Thông báo đầy đủ)
            msg = (
                f"<b>✅ XÁC NHẬN: ĐÃ NHẬN ĐỦ TIỀN</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 Mã đơn: <code>{code}</code>\n"
                f"💰 Số tiền nhận: {amount_received:,} VND\n"
                f"📦 Sản phẩm: {trade['product_name']}\n"
                f"🛡 <b>Trạng thái:</b> BOT ĐANG GIỮ TIỀN AN TOÀN\n\n"
                f"👤 Người bán: {trade['seller_name']}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🚀 <b>YÊU CẦU:</b> Người bán giao hàng ngay. Sau khi nhận hàng, người mua bấm nút xác nhận bên dưới."
            )
            
            kb = [
                [InlineKeyboardButton("✅ TÔI ĐÃ NHẬN ĐỦ HÀNG", callback_data=f"done_{code}")],
                [InlineKeyboardButton("🆘 KHIẾU NẠI / TRANH CHẤP", callback_data=f"dispute_{code}")]
            ]
            
            sent = await tg_app.bot.send_message(
                chat_id=trade['group_id'], 
                text=msg, 
                parse_mode=ParseMode.HTML, 
                reply_markup=InlineKeyboardMarkup(kb)
            )
            
            db.update_trade(code, status_msg_id=sent.message_id)
            try: await tg_app.bot.pin_chat_message(trade['group_id'], sent.message_id)
            except: pass

            # 3. Thông báo riêng cho Admin
            admin_txt = (
                f"💰 <b>BANK IN - {code}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💵 Tiền: {amount_received:,}đ\n"
                f"📂 Nhóm: {trade['group_name']}\n"
                f"👤 Mua: {trade['buyer_name']}\n"
                f"🔗 <a href='{trade['group_link']}'>Đi tới nhóm</a>"
            )
            await tg_app.bot.send_message(CONFIG['admin_id'], admin_txt, parse_mode=ParseMode.HTML)

        else:
            # Thông báo nếu thiếu tiền
            await tg_app.bot.send_message(
                trade['group_id'], 
                f"⚠️ <b>THÔNG BÁO:</b> Hệ thống nhận được {amount_received:,}đ cho đơn {code}.\n"
                f"Tuy nhiên số tiền này <b>CHƯA ĐỦ</b> (Cần {total_needed:,}đ).\n"
                f"Vui lòng chuyển thêm phần còn thiếu với cùng nội dung chuyển khoản."
            )
    except Exception as e:
        logger.error(f"Lỗi process_paid: {e}")

# ==========================================================
#                      COMMAND HANDLERS
# ==========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ Thêm Bot Vào Nhóm", url=f"https://t.me/{(await context.bot.get_me()).username}?startgroup=true")],
        [InlineKeyboardButton("👨‍💻 Admin", url=f"https://t.me/{CONFIG['admin_handle'][1:]}")]
    ]
    txt = (
        f"<b>💎 HỆ THỐNG TRUNG GIAN AUTO V16 PRO</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Chào mừng <b>{update.effective_user.full_name}</b>\n"
        f"Uy tín hiện tại: <b>{db.get_reputation(update.effective_user.id)} đơn</b>\n\n"
        f"Sử dụng lệnh <code>/taogdtg</code> trong nhóm để bắt đầu."
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

async def cmd_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bật/Tắt bảo trì (Chỉ Admin)"""
    if update.effective_user.id != CONFIG['admin_id']: return
    if not context.args:
        return await update.message.reply_text("Cú pháp: `/maintenance on` hoặc `/maintenance off`", parse_mode=ParseMode.HTML)
    
    mode = context.args[0].lower()
    CONFIG['maintenance'] = (mode == "on")
    status_text = "BẬT" if CONFIG['maintenance'] else "TẮT"
    await update.message.reply_text(f"⚙️ Đã {status_text} chế độ bảo trì hệ thống.")

async def cmd_taogdtg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if CONFIG['maintenance']:
        return await update.message.reply_text("⚠️ Hệ thống đang bảo trì để nâng cấp. Vui lòng quay lại sau.")
    
    if update.effective_chat.type == "private":
        return await update.message.reply_text("❌ Vui lòng sử dụng lệnh này trong Nhóm giao dịch.")

    try:
        raw = update.message.text.replace("/taogdtg", "").strip()
        parts = [p.strip() for p in raw.split("|")]
        
        if len(parts) < 3:
            raise ValueError("Thiếu thông tin")

        amount = int(re.sub(r"\D", "", parts[0]))
        product = parts[1]
        seller = parts[2]
        
        code = f"GD{int(datetime.now().timestamp())}"
        fee = calculate_fee(amount)
        total = amount + fee
        
        # Link nhóm
        try: group_link = await update.effective_chat.export_invite_link()
        except: group_link = "N/A"

        db.create_trade({
            "code": code, "group_id": update.effective_chat.id, "group_name": update.effective_chat.title,
            "group_link": group_link, "buyer_id": update.effective_user.id, "buyer_name": update.effective_user.full_name,
            "buyer_user": f"@{update.effective_user.username}", "seller_name": seller,
            "amount": amount, "fee": fee, "total_pay": total, "product_name": product
        })

        qr = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={total}&addInfo={code}&accountName={CONFIG['bank_owner'].replace(' ', '%20')}"
        
        txt = (
            f"<b>🤝 ĐƠN GIAO DỊCH MỚI: {code}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>Sản phẩm:</b> {product}\n"
            f"👤 <b>Người Bán:</b> {seller}\n"
            f"👤 <b>Người Mua:</b> {update.effective_user.full_name} (Uy tín: {db.get_reputation(update.effective_user.id)})\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 Tiền hàng: {amount:,} VND\n"
            f"⚙️ Phí dịch vụ: {fee:,} VND\n"
            f"💳 <b>TỔNG THANH TOÁN:</b> <code>{total:,}</code> VND\n"
            f"📝 Nội dung CK: <code>{code}</code>\n\n"
            f"{CONFIG['aml_note']}"
        )

        kb = [[InlineKeyboardButton("❌ Hủy Đơn", callback_data=f"cancel_{code}")]]
        msg = await update.message.reply_photo(photo=qr, caption=txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        db.update_trade(code, qr_msg_id=msg.message_id)
        try: await msg.pin()
        except: pass

    except Exception as e:
        await update.message.reply_text(
            "❌ <b>Sai cú pháp tạo đơn!</b>\n\nSử dụng: <code>/taogdtg SốTiền | SảnPhẩm | @NgườiBán</code>\n"
            "Ví dụ: <code>/taogdtg 500000 | Thue Cloud | @nth_dev</code>", 
            parse_mode=ParseMode.HTML
        )

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check trạng thái đơn hàng"""
    if not context.args:
        return await update.message.reply_text("Cú pháp: `/check GD123456`")
    
    code = context.args[0].upper()
    trade = db.get_trade(code)
    if not trade:
        return await update.message.reply_text("❌ Không tìm thấy mã đơn này.")
        
    txt = (
        f"🔍 <b>THÔNG TIN ĐƠN: {code}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🛡 Trạng thái: <b>{trade['status']}</b>\n"
        f"📦 Sản phẩm: {trade['product_name']}\n"
        f"💰 Số tiền: {trade['amount']:,} VND\n"
        f"👥 Mua: {trade['buyer_name']} | Bán: {trade['seller_name']}\n"
        f"⏰ Tạo lúc: {trade['created_at']}"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Seller gửi STK để rút tiền"""
    if len(context.args) < 2:
        return await update.message.reply_text("❌ Cú pháp: <code>/bank [MãGD] [STK Bank Tên]</code>", parse_mode=ParseMode.HTML)

    code, info = context.args[0].upper(), " ".join(context.args[1:])
    trade = db.get_trade(code)
    
    if not trade: return await update.message.reply_text("❌ Mã đơn không tồn tại.")
    
    # Chỉ cho phép Seller (người được tag) rút tiền
    current_username = f"@{update.effective_user.username}".lower()
    if current_username != trade['seller_name'].lower():
        return await update.message.reply_text(f"⛔ Quyền rút tiền thuộc về: <b>{trade['seller_name']}</b>", parse_mode=ParseMode.HTML)

    if trade['status'] == Status.BUYER_DONE:
        db.update_trade(code, status=Status.PAYOUT_WAIT, seller_bank_info=info)
        db.add_log(code, "YÊU CẦU RÚT", info)
        
        kb = [[InlineKeyboardButton("✅ XÁC NHẬN ĐÃ BANK", callback_data=f"ap_{code}")]]
        admin_msg = (
            f"🏛 <b>YÊU CẦU GIẢI NGÂN: {code}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Số tiền: {trade['amount']:,}đ\n"
            f"💳 STK nhận: {info}\n"
            f"📂 Nhóm: {trade['group_name']}\n"
            f"🔗 <a href='{trade['group_link']}'>Link nhóm</a>"
        )
        await context.bot.send_message(CONFIG['admin_id'], admin_msg, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        await update.message.reply_text("✅ Đã gửi thông tin cho Admin. Vui lòng chờ giải ngân.")
    else:
        await update.message.reply_text(f"❌ Đơn hàng chưa ở trạng thái có thể rút tiền.\nHiện tại: {trade['status']}")

# ==========================================================
#                      CALLBACK HANDLERS
# ==========================================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    username = f"@{update.effective_user.username}".lower()

    # --- Người mua xác nhận nhận hàng ---
    if data.startswith("done_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade and user_id == trade['buyer_id']:
            db.update_trade(code, status=Status.BUYER_DONE)
            db.add_log(code, "XÁC NHẬN", "Khách đã nhận hàng")
            
            txt = (
                f"<b>📦 ĐƠN {code} ĐÃ NHẬN HÀNG</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Người bán <b>{trade['seller_name']}</b> vui lòng gõ lệnh sau để rút tiền:\n"
                f"<code>/bank {code} [STK Tên Ngân Hàng]</code>"
            )
            await query.edit_message_text(text=txt, parse_mode=ParseMode.HTML)
        else:
            await query.answer("⛔ Bạn không có quyền xác nhận đơn này!", show_alert=True)

    # --- Khiếu nại ---
    elif data.startswith("dispute_"):
        code = data.split("_")[1]
        trade = db.get_trade(code)
        if trade and (user_id == trade['buyer_id'] or username == trade['seller_name'].lower()):
            db.update_trade(code, status=Status.DISPUTED)
            db.add_log(code, "KHIẾU NẠI", f"Bởi: {username}")
            
            await query.edit_message_text(
                f"🆘 <b>ĐƠN {code} ĐANG KHIẾU NẠI</b>\n"
                f"Hệ thống đã tạm dừng mọi thao tác. Admin {CONFIG['admin_handle']} sẽ vào nhóm hỗ trợ.",
                parse_mode=ParseMode.HTML
            )
            # Báo Admin ngay
            await context.bot.send_message(
                CONFIG['admin_id'], 
                f"🆘 <b>KHIẾU NẠI MỚI: {code}</b>\nLink nhóm: {trade['group_link']}",
                parse_mode=ParseMode.HTML
            )
        else:
            await query.answer("⛔ Bạn không liên quan đến đơn này!", show_alert=True)

    # --- Admin giải ngân ---
    elif data.startswith("ap_"):
        if user_id != CONFIG['admin_id']: return
        code = data.split("_")[1]
        trade = db.get_trade(code)
        
        # Tính thời gian hoàn thành
        start_time = datetime.strptime(trade['created_at'], "%Y-%m-%d %H:%M:%S")
        end_time = datetime.now()
        duration = str(end_time - start_time).split(".")[0] # Định dạng X:XX:XX
        
        db.update_trade(code, status=Status.COMPLETED, completed_at=end
