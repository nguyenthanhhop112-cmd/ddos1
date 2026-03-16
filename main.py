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

DB_FILE = "system_v10.sqlite3"

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
                seller_id INTEGER, seller_name TEXT, 
                amount INTEGER, fee INTEGER, total_pay INTEGER,
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
#                      WEBHOOK SEPAY (NHẬN TIỀN TỰ ĐỘNG)
# ==========================================================
@app.post("/webhook")
async def sepay_webhook(request: Request):
    try:
        data = await request.json()
        content = data.get("content", "").upper()
        amount_in = int(data.get("amount_in", 0))
        logger.info(f"Webhook Triggered: ND={content} | TIỀN={amount_in}")
        
        match = re.search(r"GD\d+", content)
        if match and amount_in > 0:
            code = match.group()
            # Bắn logic vào task chạy ngầm, webhook trả về success ngay để SePay ko báo lỗi
            asyncio.create_task(process_paid_invoice(code, amount_in))
        
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Lỗi Webhook: {e}")
        return {"status": "error"}

async def process_paid_invoice(code, amount):
    trade = db.get_trade(code)
    # Lọc điều kiện: Chỉ xử lý nếu đơn tồn tại, trạng thái CHỜ và tiền chuyển >= tổng tiền
    if not trade or trade['status'] != Status.PENDING or amount < trade['total_pay']:
        return

    db.update_trade(code, status=Status.HOLDING)
    
    # Gỡ ghim tin nhắn QR code cũ
    try:
        await tg_app.bot.unpin_chat_message(chat_id=trade['group_id'], message_id=trade['qr_msg_id'])
    except: pass

    # UI Nhận Tiền Thành Công (HTML)
    msg = f"""<b>✅ GIAO DỊCH {code} ĐÃ NHẬN TIỀN</b>
━━━━━━━━━━━━━━━━━━━━
📦 <b>Sản phẩm:</b> {trade['product_name']}
💰 <b>Số tiền nhận:</b> {amount:,} VND
🛡 <b>Trạng thái:</b> BOT ĐANG GIỮ TIỀN AN TOÀN

👤 <b>Người mua:</b> {trade['buyer_name']}
👤 <b>Người bán:</b> {trade['seller_name']}
━━━━━━━━━━━━━━━━━━━━
🚀 <b>YÊU CẦU:</b> Mời người bán tiến hành giao hàng cho người mua.
⚠️ <i>Sau khi nhận đủ hàng và kiểm tra chính xác, người mua vui lòng bấm nút xác nhận bên dưới!</i>"""

    btn = [[InlineKeyboardButton("✅ XÁC NHẬN ĐÃ NHẬN HÀNG (Dành cho người mua)", callback_data=f"done_{code}")]]
    
    try:
        sent_msg = await tg_app.bot.send_message(
            chat_id=trade['group_id'], 
            text=msg, 
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(btn)
        )
        db.update_trade(code, status_msg_id=sent_msg.message_id)
        # Ghim tin nhắn trạng thái mới
        await tg_app.bot.pin_chat_message(chat_id=trade['group_id'], message_id=sent_msg.message_id)
    except Exception as e:
        logger.error(f"Lỗi khi gửi thông báo vào group: {e}")

# ==========================================================
#                      BOT COMMANDS & GIAO DIỆN
# ==========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_info = await context.bot.get_me()
    keyboard = [
        [InlineKeyboardButton("➕ Thêm Bot Vào Nhóm Giao Dịch", url=f"https://t.me/{bot_info.username}?startgroup=true")],
        [InlineKeyboardButton("📖 Hướng Dẫn Sử Dụng", callback_data="ui_help")],
        [InlineKeyboardButton("👨‍💻 Liên Hệ Admin Đội Ngũ", url=f"https://t.me/{CONFIG['admin_handle'][1:]}")]
    ]
    
    txt = f"""<b>⚡ HỆ THỐNG TRUNG GIAN TỰ ĐỘNG CAO CẤP</b>
━━━━━━━━━━━━━━━━━━━━
Chào mừng bạn đến với nền tảng Giao Dịch Trung Gian an toàn tuyệt đối.

<b>💎 TÍNH NĂNG NỔI BẬT:</b>
• Tự động nhận diện thanh toán Bank chỉ trong 3 giây.
• Bot giữ tiền an toàn tuyệt đối cho người mua.
• Giải ngân siêu tốc cho người bán.
• Tránh 100% rủi ro lừa đảo (Scam).

<i>Sử dụng các nút bên dưới để khám phá hệ thống!</i>"""
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def cmd_taogdtg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return await update.message.reply_text("❌ Lệnh này chỉ hoạt động trong Nhóm Giao Dịch!")
    
    try:
        text = update.message.text.replace("/taogdtg", "").strip()
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 3: raise ValueError()
        
        amount = int(re.sub(r"\D", "", parts[0]))
        product = parts[1]
        seller = parts[2]
        
        code = f"GD{int(datetime.now().timestamp())}"
        fee = max(CONFIG['fee_min'], int(amount * CONFIG['fee_percent']))
        total = amount + fee

        db.create_trade({
            "code": code, "group_id": update.effective_chat.id, "group_name": update.effective_chat.title,
            "buyer_id": update.effective_user.id, "buyer_name": update.effective_user.full_name,
            "buyer_user": f"@{update.effective_user.username}", "seller_name": seller,
            "amount": amount, "fee": fee, "total_pay": total, "product_name": product
        })

        qr_url = f"https://img.vietqr.io/image/{CONFIG['bank_bin']}-{CONFIG['bank_stk']}-compact2.png?amount={total}&addInfo={code}&accountName={CONFIG['bank_owner'].replace(' ', '%20')}"
        
        txt = f"""<b>🤝 YÊU CẦU GIAO DỊCH TRUNG GIAN</b>
━━━━━━━━━━━━━━━━━━━━
🆔 <b>Mã giao dịch:</b> <code>{code}</code>
📦 <b>Sản phẩm:</b> {product}
👤 <b>Người bán:</b> {seller}
👤 <b>Người mua:</b> {update.effective_user.full_name}
━━━━━━━━━━━━━━━━━━━━
💵 <b>Giá trị SP:</b> {amount:,} VND
⚙️ <b>Phí trung gian:</b> {fee:,} VND
💳 <b>TỔNG CẦN THANH TOÁN:</b> <code>{total:,}</code> VND
📝 <b>NỘI DUNG CHUYỂN KHOẢN:</b> <code>{code}</code>

⚠️ <i>Vui lòng quét mã QR hoặc chuyển khoản đúng số tiền và nội dung. Hệ thống sẽ tự động xác nhận trong 3s-5s!</i>"""

        msg = await update.message.reply_photo(photo=qr_url, caption=txt, parse_mode=ParseMode.HTML)
        db.update_trade(code, qr_msg_id=msg.message_id)
        try: await msg.pin() 
        except: pass
        
    except Exception:
        await update.message.reply_text("<b>❌ SAI CÚ PHÁP TẠO ĐƠN!</b>\n👉 <b>Mẫu chuẩn:</b> <code>/taogdtg Số_tiền | Tên_sản_phẩm | @Nguoi_ban</code>", parse_mode=ParseMode.HTML)

# ==========================================================
#                      LOGIC NÚT BẤM (ANTI-TREO)
# ==========================================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    try:
        # NÚT: XÁC NHẬN NHẬN HÀNG
        if data.startswith("done_"):
            code = data.split("_")[1]
            trade = db.get_trade(code)
            
            if not trade:
                return await query.answer("❌ Đơn hàng này không còn tồn tại trong hệ thống!", show_alert=True)
            
            if user_id != trade['buyer_id']:
                return await query.answer("⛔ CHỈ NGƯỜI MUA CHÍNH CHỦ MỚI ĐƯỢC BẤM NÚT NÀY!", show_alert=True)
                
            if trade['status'] != Status.HOLDING:
                return await query.answer("⚠️ Giao dịch này đã được xác nhận hoặc đã kết thúc!", show_alert=True)

            # Xử lý thành công
            db.update_trade(code, status=Status.BUYER_DONE)
            await query.answer("✅ XÁC NHẬN THÀNH CÔNG! Đang gọi người bán...")
            
            try: await context.bot.unpin_chat_message(chat_id=trade['group_id'], message_id=trade['status_msg_id'])
            except: pass
            
            txt = f"""<b>📦 XÁC NHẬN NHẬN HÀNG THÀNH CÔNG</b>
━━━━━━━━━━━━━━━━━━━━
🆔 <b>Mã đơn:</b> <code>{code}</code>
✅ Người mua đã xác nhận nhận đầy đủ sản phẩm.

<b>🔔 BƯỚC CUỐI - NHẬN TIỀN:</b>
Mời người bán {trade['seller_name']} gửi thông tin ngân hàng để Admin giải ngân.
👉 <b>Gõ lệnh theo cú pháp:</b>
<code>/bank {code} Số_tài_khoản Tên_ngân_hàng Tên_chủ_thẻ</code>"""
            
            await query.edit_message_caption(caption=txt, parse_mode=ParseMode.HTML) if query.message.photo else await query.edit_message_text(text=txt, parse_mode=ParseMode.HTML)

        # NÚT: ADMIN PAYOUT
        elif data.startswith("adminpayout_"):
            if user_id != CONFIG['admin_id']:
                return await query.answer("⛔ Nút này chỉ dành cho Admin!", show_alert=True)
            
            code = data.split("_")[1]
            trade = db.get_trade(code)
            if trade and trade['status'] == Status.PAYOUT_WAIT:
                db.update_trade(code, status=Status.COMPLETED)
                await query.answer("✅ ĐÃ CHỐT ĐƠN!")
                
                txt_admin = f"✅ <b>ĐÃ GIẢI NGÂN CHO ĐƠN:</b> <code>{code}</code>"
                await query.edit_message_text(text=txt_admin, parse_mode=ParseMode.HTML)
                
                txt_group = f"""<b>🎉 GIAO DỊCH HOÀN TẤT TUYỆT ĐỐI</b>
━━━━━━━━━━━━━━━━━━━━
🆔 <b>Mã đơn:</b> <code>{code}</code>
💸 Admin đã giải ngân thành công cho người bán.
🤝 Cảm ơn các bạn đã sử dụng dịch vụ trung gian!"""
                await context.bot.send_message(chat_id=trade['group_id'], text=txt_group, parse_mode=ParseMode.HTML)

        # UI HƯỚNG DẪN
        elif data == "ui_help":
            await query.answer()
            txt = f"""<b>📖 HƯỚNG DẪN QUY TRÌNH TRUNG GIAN</b>
━━━━━━━━━━━━━━━━━━━━
<b>Bước 1:</b> Tại nhóm, gửi lệnh tạo đơn:
<code>/taogdtg Số_tiền | Sản_phẩm | @Nguoi_ban</code>
<b>Bước 2:</b> Người mua quét mã QR thanh toán (Hệ thống tự động báo nhận tiền trong 3s).
<b>Bước 3:</b> Người bán tiến hành giao hàng.
<b>Bước 4:</b> Người mua nhấn nút <b>"Xác nhận đã nhận hàng"</b>.
<b>Bước 5:</b> Người bán gửi STK bằng lệnh:
<code>/bank Mã_đơn STK Tên_Bank...</code>
<b>Bước 6:</b> Admin giải ngân và chốt đơn."""
            btn = [[InlineKeyboardButton("🔙 Quay Lại", callback_data="ui_back")]]
            await query.edit_message_text(text=txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(btn))
            
        elif data == "ui_back":
            await query.answer()
            await cmd_start(update, context) # Gọi lại hàm start để reset menu

    except Exception as e:
        logger.error(f"Lỗi Callback Data: {e}")
        try: await query.answer("❌ Đã xảy ra lỗi, vui lòng thử lại sau!", show_alert=True)
        except: pass

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2: 
        return await update.message.reply_text("❌ Thiếu thông tin! Gõ: `/bank Mã_đơn STK Ngân_hàng`", parse_mode=ParseMode.MARKDOWN)
    
    code = context.args[0].upper()
    bank_info = " ".join(context.args[1:])
    trade = db.get_trade(code)
    
    if not trade:
        return await update.message.reply_text("❌ Mã giao dịch không tồn tại!")
        
    if trade['status'] != Status.BUYER_DONE:
        return await update.message.reply_text("❌ Đơn chưa đến bước giải ngân hoặc đã giải ngân xong!")

    db.update_trade(code, status=Status.PAYOUT_WAIT, seller_bank_info=bank_info)
    
    # Gửi form cho Admin duyệt
    adm_btn = [[InlineKeyboardButton("✅ ADMIN ĐÃ CHUYỂN TIỀN (CHỐT ĐƠN)", callback_data=f"adminpayout_{code}")]]
    adm_txt = f"""<b>🏛 YÊU CẦU GIẢI NGÂN (PAYOUT)</b>
━━━━━━━━━━━━━━━━━━━━
🆔 <b>Đơn:</b> <code>{code}</code>
💰 <b>Số tiền cần chuyển:</b> {trade['amount']:,} VND
💳 <b>Tài khoản nhận:</b> <code>{bank_info}</code>
📍 <b>Nhóm GD:</b> {trade['group_name']}
━━━━━━━━━━━━━━━━━━━━
⚠️ <i>Bank xong hãy bấm xác nhận bên dưới!</i>"""
    
    await context.bot.send_message(chat_id=CONFIG['admin_id'], text=adm_txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(adm_btn))
    await update.message.reply_text("✅ <b>ĐÃ GỬI THÔNG TIN CHO ADMIN!</b>\nVui lòng chờ giải ngân trong giây lát.", parse_mode=ParseMode.HTML)

# ==========================================================
#                      KHỞI CHẠY HỆ THỐNG
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
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main_runner())
    except (KeyboardInterrupt, SystemExit):
        pass
    
