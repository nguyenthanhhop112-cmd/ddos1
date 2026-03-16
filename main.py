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

DB_FILE = "system_v11.sqlite3" # Đổi tên DB để làm mới hoàn toàn, tránh kẹt dữ liệu cũ

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
        logger.info(f"WEBHOOK BẮN VỀ: Nội dung='{content}' | Tiền={amount_in}")
        
        # Bắt chính xác mã GD, bất chấp khoảng trắng hay rác phía sau
        match = re.search(r"GD\d+", content)
        if match and amount_in > 0:
            code = match.group()
            asyncio.create_task(process_paid_invoice(code, amount_in))
        
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Lỗi Webhook Crash: {e}")
        return {"status": "error"}

async def process_paid_invoice(code, amount):
    trade = db.get_trade(code)
    
    # Bỏ qua nếu không có đơn này hoặc đơn đã qua bước nhận tiền
    if not trade or trade['status'] != Status.PENDING:
        return

    # LOGIC CHẶT CHẼ BÁO LỖI THIẾU TIỀN RÕ RÀNG VÀO ĐÚNG GROUP
    if amount < trade['total_pay']:
        thieu = trade['total_pay'] - amount
        msg_error = f"""<b>⚠️ CẢNH BÁO: CHUYỂN THIẾU TIỀN ĐƠN {code}</b>
━━━━━━━━━━━━━━━━━━━━
📦 <b>Sản phẩm:</b> {trade['product_name']}
💰 <b>Tổng cần thanh toán (Bao gồm phí):</b> <code>{trade['total_pay']:,}</code> VND
📥 <b>Hệ thống thực nhận:</b> <code>{amount:,}</code> VND
❌ <b>SỐ TIỀN CÒN THIẾU:</b> <code>{thieu:,}</code> VND

👤 <b>Người mua:</b> {trade['buyer_name']}
⚠️ <i>Vui lòng chuyển khoản thêm đúng số tiền còn thiếu với nội dung <code>{code}</code> để hệ thống tự động chốt đơn! Giao dịch đang bị tạm giữ.</i>"""
        try:
            await tg_app.bot.send_message(chat_id=trade['group_id'], text=msg_error, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Lỗi gửi tin báo thiếu tiền vào group {trade['group_id']}: {e}")
        return

    # LOGIC NHẬN ĐỦ TIỀN - CHUYỂN TRẠNG THÁI HOLDING
    db.update_trade(code, status=Status.HOLDING)
    
    # Cố gắng gỡ ghim mã QR cũ cho đỡ rác nhóm
    try:
        await tg_app.bot.unpin_chat_message(chat_id=trade['group_id'], message_id=trade['qr_msg_id'])
    except: pass

    # In ra thông báo nhận đủ tiền và tạo nút xác nhận cho người mua
    msg_success = f"""<b>✅ GIAO DỊCH {code} ĐÃ NHẬN ĐỦ TIỀN</b>
━━━━━━━━━━━━━━━━━━━━
📦 <b>Sản phẩm:</b> {trade['product_name']}
💰 <b>Số tiền hệ thống nhận:</b> {amount:,} VND
🛡 <b>Trạng thái:</b> BOT ĐANG GIỮ TIỀN AN TOÀN

👤 <b>Người mua:</b> {trade['buyer_name']}
👤 <b>Người bán:</b> {trade['seller_name']}
━━━━━━━━━━━━━━━━━━━━
🚀 <b>YÊU CẦU:</b> Mời người bán tiến hành giao hàng cho người mua.
⚠️ <i>LƯU Ý QUAN TRỌNG: Chỉ khi nào người mua nhận đủ hàng, hãy bấm nút XÁC NHẬN bên dưới để Admin tiến hành giải ngân!</i>"""

    btn = [[InlineKeyboardButton("✅ TÔI ĐÃ NHẬN ĐỦ HÀNG (Người mua bấm)", callback_data=f"done_{code}")]]
    
    try:
        sent_msg = await tg_app.bot.send_message(
            chat_id=trade['group_id'], 
            text=msg_success, 
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(btn)
        )
        db.update_trade(code, status_msg_id=sent_msg.message_id)
        # Ghim tin nhắn trạng thái mới lên để mọi người thấy
        await tg_app.bot.pin_chat_message(chat_id=trade['group_id'], message_id=sent_msg.message_id)
    except Exception as e:
        logger.error(f"Lỗi khi gửi thông báo thành công vào group {trade['group_id']}: {e}")

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
    # Chỉ cho phép tạo GD trong nhóm
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
        # Logic tính phí
        fee = max(CONFIG['fee_min'], int(amount * CONFIG['fee_percent']))
        total = amount + fee

        # Lưu thẳng ID của cái nhóm vừa gõ lệnh để sau này Webhook bắn về đúng nhóm đó
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
        # Tự động ghim QR code
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
        # LOGIC NÚT 1: NGƯỜI MUA XÁC NHẬN ĐÃ NHẬN HÀNG
        if data.startswith("done_"):
            code = data.split("_")[1]
            trade = db.get_trade(code)
            
            if not trade:
                return await query.answer("❌ Đơn hàng này không còn tồn tại trong hệ thống!", show_alert=True)
            
            # Cấm người ngoài bấm linh tinh
            if user_id != trade['buyer_id']:
                return await query.answer("⛔ CHỈ NGƯỜI MUA CHÍNH CHỦ MỚI ĐƯỢC BẤM NÚT NÀY!", show_alert=True)
                
            if trade['status'] != Status.HOLDING:
                return await query.answer("⚠️ Giao dịch này đã được xác nhận hoặc đã kết thúc rồi!", show_alert=True)

            # Xử lý thành công, chuyển sang bước yêu cầu Bank
            db.update_trade(code, status=Status.BUYER_DONE)
            await query.answer("✅ XÁC NHẬN THÀNH CÔNG! Đang gọi người bán...")
            
            # Gỡ ghim tin nhắn yêu cầu chờ hàng
            try: await context.bot.unpin_chat_message(chat_id=trade['group_id'], message_id=trade['status_msg_id'])
            except: pass
            
            txt_done = f"""<b>📦 XÁC NHẬN NHẬN HÀNG THÀNH CÔNG</b>
━━━━━━━━━━━━━━━━━━━━
🆔 <b>Mã đơn:</b> <code>{code}</code>
✅ Người mua đã xác nhận nhận đầy đủ sản phẩm.

<b>🔔 BƯỚC CUỐI - NHẬN TIỀN (Dành cho người bán):</b>
Mời người bán {trade['seller_name']} gửi thông tin ngân hàng để Admin giải ngân.
👉 <b>Người bán gõ lệnh theo cú pháp:</b>
<code>/bank {code} Số_tài_khoản Tên_ngân_hàng Tên_chủ_thẻ</code>"""
            
            if query.message.photo:
                await query.edit_message_caption(caption=txt_done, parse_mode=ParseMode.HTML)
            else:
                await query.edit_message_text(text=txt_done, parse_mode=ParseMode.HTML)

        # LOGIC NÚT 2: ADMIN XÁC NHẬN ĐÃ GIẢI NGÂN
        elif data.startswith("adminpayout_"):
            if user_id != CONFIG['admin_id']:
                return await query.answer("⛔ Bạn không phải là Admin! Tránh ra!", show_alert=True)
            
            code = data.split("_")[1]
            trade = db.get_trade(code)
            
            if trade and trade['status'] == Status.PAYOUT_WAIT:
                db.update_trade(code, status=Status.COMPLETED)
                await query.answer("✅ ĐÃ CHỐT ĐƠN & GỬI THÔNG BÁO VÀO NHÓM!")
                
                # Cập nhật tin nhắn trong DM của Admin
                txt_admin = f"✅ <b>ĐÃ GIẢI NGÂN THÀNH CÔNG CHO ĐƠN:</b> <code>{code}</code>"
                await query.edit_message_text(text=txt_admin, parse_mode=ParseMode.HTML)
                
                # Bắn thông báo chốt đơn về đúng Nhóm GD ban đầu
                txt_group = f"""<b>🎉 GIAO DỊCH HOÀN TẤT TUYỆT ĐỐI</b>
━━━━━━━━━━━━━━━━━━━━
🆔 <b>Mã đơn:</b> <code>{code}</code>
📦 <b>Sản phẩm:</b> {trade['product_name']}
💸 Admin đã giải ngân thành công số tiền <code>{trade['amount']:,}</code> VND cho người bán.

🤝 Cảm ơn các bạn đã tin tưởng sử dụng dịch vụ trung gian của chúng tôi!"""
                await context.bot.send_message(chat_id=trade['group_id'], text=txt_group, parse_mode=ParseMode.HTML)

        # UI HƯỚNG DẪN Ở MENU START
        elif data == "ui_help":
            await query.answer()
            txt_help = f"""<b>📖 HƯỚNG DẪN QUY TRÌNH TRUNG GIAN</b>
━━━━━━━━━━━━━━━━━━━━
<b>Bước 1:</b> Tại nhóm GD, gửi lệnh tạo đơn:
<code>/taogdtg Số_tiền | Sản_phẩm | @Nguoi_ban</code>
<b>Bước 2:</b> Người mua quét mã QR thanh toán (Hệ thống tự động báo nhận tiền trong 3s).
<b>Bước 3:</b> Người bán tiến hành giao hàng cho người mua.
<b>Bước 4:</b> Nhận đủ hàng, Người mua nhấn nút <b>"Tôi đã nhận đủ hàng"</b>.
<b>Bước 5:</b> Người bán gửi STK nhận tiền bằng lệnh:
<code>/bank Mã_đơn STK Tên_Bank...</code>
<b>Bước 6:</b> Admin chuyển tiền cho người bán và chốt đơn."""
            btn_back = [[InlineKeyboardButton("🔙 Quay Lại Menu Chính", callback_data="ui_back")]]
            await query.edit_message_text(text=txt_help, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(btn_back))
            
        elif data == "ui_back":
            await query.answer()
            await cmd_start(update, context)

    except Exception as e:
        logger.error(f"Lỗi Callback Data: {e}")
        try: await query.answer("❌ Đã xảy ra lỗi hệ thống, vui lòng thử lại sau!", show_alert=True)
        except: pass

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Chỉ cho phép gọi trong nhóm
    if update.effective_chat.type == "private":
        return await update.message.reply_text("❌ Lệnh này phải được gõ trong Nhóm Giao Dịch!")

    if len(context.args) < 2: 
        return await update.message.reply_text("❌ Thiếu thông tin!\n👉 <b>Mẫu chuẩn:</b> <code>/bank Mã_đơn STK Tên_ngân_hàng</code>", parse_mode=ParseMode.HTML)
    
    code = context.args[0].upper()
    bank_info = " ".join(context.args[1:])
    trade = db.get_trade(code)
    
    if not trade:
        return await update.message.reply_text("❌ Mã giao dịch này không tồn tại trong hệ thống!")
        
    if trade['status'] != Status.BUYER_DONE:
        return await update.message.reply_text("❌ Đơn này chưa được người mua xác nhận nhận hàng, hoặc đã được giải ngân xong rồi!")

    # Cập nhật trạng thái chờ giải ngân
    db.update_trade(code, status=Status.PAYOUT_WAIT, seller_bank_info=bank_info)
    
    # Gửi form Payout về tin nhắn riêng (DM) cho Admin để duyệt
    adm_btn = [[InlineKeyboardButton("✅ ADMIN ĐÃ CHUYỂN TIỀN (BẤM ĐỂ CHỐT ĐƠN)", callback_data=f"adminpayout_{code}")]]
    adm_txt = f"""<b>🏛 YÊU CẦU GIẢI NGÂN (PAYOUT)</b>
━━━━━━━━━━━━━━━━━━━━
🆔 <b>Mã Đơn:</b> <code>{code}</code>
💰 <b>Số tiền cần chuyển trả:</b> {trade['amount']:,} VND
💳 <b>Tài khoản nhận:</b> <code>{bank_info}</code>
📍 <b>Nhóm yêu cầu:</b> {trade['group_name']}
━━━━━━━━━━━━━━━━━━━━
⚠️ <i>Sau khi chuyển khoản xong, Admin hãy bấm nút bên dưới để hệ thống báo hoàn tất vào Group!</i>"""
    
    try:
        await context.bot.send_message(chat_id=CONFIG['admin_id'], text=adm_txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(adm_btn))
        await update.message.reply_text("✅ <b>ĐÃ GỬI THÔNG TIN BANK CHO ADMIN!</b>\nTiền sẽ được chuyển trong giây lát, vui lòng chờ.", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Lỗi gửi tin cho Admin (Admin chưa start bot?): {e}")
        await update.message.reply_text("❌ <b>Lỗi:</b> Không thể gửi tin nhắn cho Admin. Bot cần Admin chủ động inbox /start cho bot trước!")

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
                 
