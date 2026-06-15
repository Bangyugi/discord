# HƯỚNG DẪN HOSTING BOT CHRONOS DISCIPLINE 24/24

Tài liệu này hướng dẫn chi tiết cách triển khai (deploy) Discord bot của bạn lên máy chủ (Server/VPS) để hoạt động liên tục 24/7.

---

## 🛠️ Chuẩn Bị Trước Khi Deploy

Dù chọn cách nào, bạn đều cần chuẩn bị các thông tin sau từ file `.env` của mình:
- **`DISCORD_TOKEN`**: Token của Discord Bot (lấy từ [Discord Developer Portal](https://discord.com/developers/applications)).
- **Các ID kênh và Role**: Đảm bảo cấu hình các ID phòng voice, kênh log, kênh thông báo, role mod, v.v., khớp với server Discord đích.

> [!WARNING]
> Không bao giờ đưa file `.env` hoặc file database `.db` lên các kho chứa mã nguồn công khai (như GitHub Public Repository) để tránh bị lộ Token hoặc mất dữ liệu.

---

## 🚀 Cách 1: Sử dụng Docker & Docker Compose (Khuyên Dùng)

Đây là cách tốt nhất, an toàn nhất và dễ quản lý nhất trên VPS (Ubuntu, Debian, CentOS, v.v.). Database SQLite sẽ được lưu trong một ổ đĩa ảo (Volume) của Docker nên không bị mất khi cập nhật bot.

### Bước 1: Cài đặt Docker & Docker Compose trên VPS
Nếu VPS của bạn chưa cài Docker, hãy chạy các lệnh sau (trên Ubuntu):
```bash
sudo apt update
sudo apt install -y docker.io docker-compose
sudo systemctl enable --now docker
```

### Bước 2: Đưa mã nguồn lên VPS
Bạn có thể sử dụng `git clone` để kéo code từ kho chứa riêng tư (Private Repository) về VPS:
```bash
git clone <URL_KHO_MA_NGUON_CUA_BAN> discord-bot
cd discord-bot
```

### Bước 3: Cấu hình file `.env`
Tạo file `.env` trên VPS và điền các thông số cấu hình:
```bash
nano .env
```
*(Copy nội dung cấu hình từ máy cá nhân của bạn vào đây, rồi nhấn `Ctrl + O` -> `Enter` để lưu, `Ctrl + X` để thoát).*

### Bước 4: Khởi chạy Bot bằng Docker Compose
Chạy lệnh sau để Docker tự động tải ảnh Python, cài đặt thư viện và khởi động bot dưới dạng chạy ngầm (detach mode):
```bash
docker-compose up -d --build
```

### Bước 5: Các lệnh quản lý thường dùng:
- **Xem logs của bot**: `docker-compose logs -f`
- **Dừng bot**: `docker-compose down`
- **Khởi động lại bot**: `docker-compose restart`
- **Xem trạng thái container**: `docker-compose ps`

---

## ⚙️ Cách 2: Chạy trực tiếp trên VPS bằng Systemd (Không dùng Docker)

Nếu không muốn dùng Docker, bạn có thể chạy trực tiếp bằng Python trên VPS và quản lý thông qua công cụ giám sát dịch vụ của Linux (`systemd`).

### Bước 1: Cài đặt Python và Pip trên VPS
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
```

### Bước 2: Clone dự án và tạo môi trường ảo (venv)
```bash
git clone <URL_KHO_MA_NGUON_CUA_BAN> discord-bot
cd discord-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Bước 3: Cấu hình `.env`
Tạo file cấu hình `.env` giống như hướng dẫn ở Cách 1.

### Bước 4: Thiết lập Systemd Service
Chúng tôi đã tạo sẵn file cấu hình dịch vụ mẫu là [chronos-bot.service](file:///c:/Users/ADMIN/Desktop/discord/chronos-bot.service). Bạn cần làm như sau:
1. Copy file dịch vụ vào thư mục hệ thống:
   ```bash
   sudo cp chronos-bot.service /etc/systemd/system/chronos-bot.service
   ```
2. Mở file ra chỉnh sửa lại đường dẫn thư mục nguồn (`WorkingDirectory`) và User chạy bot cho đúng với VPS của bạn:
   ```bash
   sudo nano /etc/systemd/system/chronos-bot.service
   ```
3. Nạp lại cấu hình systemd và kích hoạt dịch vụ chạy cùng hệ thống:
   ```bash
   sudo systemctl daemon-reload
   # Bật tự động khởi chạy khi bật VPS
   sudo systemctl enable chronos-bot
   # Khởi động bot ngay lập tức
   sudo systemctl start chronos-bot
   ```

### Bước 5: Lệnh quản lý Systemd:
- **Kiểm tra trạng thái bot**: `sudo systemctl status chronos-bot`
- **Xem logs thời gian thực**: `journalctl -u chronos-bot -f`
- **Restart bot**: `sudo systemctl restart chronos-bot`
- **Dừng bot**: `sudo systemctl stop chronos-bot`

---

## ☁️ Cách 3: Sử dụng các dịch vụ Cloud PaaS (Railway, Render, Koyeb)

Nếu bạn không muốn thuê VPS và cài đặt dòng lệnh phức tạp, bạn có thể dùng các dịch vụ Cloud PaaS.

> [!IMPORTANT]
> Dự án sử dụng SQLite (lưu database thành file cục bộ). Khi chạy trên cloud như Railway/Render, mỗi lần bot khởi động lại hoặc bạn deploy code mới, file database mặc định sẽ bị xóa sạch nếu không cấu hình ổ đĩa gắn ngoài (Volume).

### A. Triển khai trên Railway (Khuyên dùng trong các PaaS)
1. Truy cập [Railway.app](https://railway.app/) và đăng nhập bằng GitHub.
2. Tạo **New Project** -> chọn **Deploy từ GitHub repo** chứa mã nguồn bot của bạn.
3. Trong phần cấu hình của project:
   - Thêm các biến môi trường (Variables) tương ứng với các trường trong file `.env`.
   - **Tạo Volume**: Vào thẻ `Settings` của service bot -> Thêm một **Volume** (ví dụ tên là `bot_volume` và Mount Path là `/app/data`).
   - Cấu hình biến môi trường `DATABASE_URL` là: `sqlite+aiosqlite:///data/chronos_discipline.db`.
4. Railway sẽ tự động build từ `Dockerfile` và deploy bot lên hoạt động 24/24.

### B. Triển khai trên Render.com (Vượt giới hạn ngủ đông của gói Free)
Render có một cơ chế: Các ứng dụng chạy trên gói miễn phí (**Free Web Service**) sẽ tự động "ngủ đông" (đóng băng) sau 15 phút nếu không nhận được bất kỳ lượt truy cập HTTP nào. 

Để khắc phục và "lách luật" giữ bot chạy 24/7 hoàn toàn miễn phí, chúng tôi đã tích hợp sẵn một **Web Server phụ (Keep-Alive)** chạy song song trong bot thông qua thư viện `aiohttp`.

#### Bước 1: Khởi tạo trên Render
1. Đăng nhập vào [Render.com](https://render.com/).
2. Chọn **New** -> **Web Service** (Chọn Web Service thay vì Private Service để được sử dụng gói Free).
3. Kết nối với kho mã nguồn GitHub chứa bot của bạn.
4. Thiết lập cấu hình dự án:
   - **Runtime**: `Docker` (Render sẽ tự động đọc file `Dockerfile` đã được tạo).
   - **Instance Type**: `Free`.
5. Vào mục **Advanced** để cấu hình:
   - **Thêm Disk** (Volume): Nhấp chọn *Add Disk*, đặt tên tùy ý và chọn Mount Path là `/app/data` (Kích thước tối thiểu 1GB là quá đủ cho SQLite).
   - **Environment Variables**:
     - Thêm `DATABASE_URL` = `sqlite+aiosqlite:///data/chronos_discipline.db`.
     - Thêm các biến môi trường khác như `DISCORD_TOKEN`, `PHONG_LAM_VIEC_ID`, v.v. (từ file `.env`).
6. Nhấn **Create Web Service** và chờ Render hoàn thành build. Sau khi chạy xong, bạn sẽ thấy đường dẫn web dạng: `https://ten-ung-dung-cua-ban.onrender.com`.

#### Bước 2: Thiết lập ping tự động ("Lách luật" chống ngủ đông)
Vì Render yêu cầu dịch vụ Web nhận request để giữ thức, bạn hãy dùng dịch vụ kiểm tra trạng thái miễn phí của bên thứ ba để tự động "gõ cửa" bot mỗi 5-10 phút:

1. Truy cập [UptimeRobot.com](https://uptimerobot.com/) hoặc [Cron-job.org](https://cron-job.org/) (đều có gói miễn phí).
2. Tạo một Monitor mới:
   - **Monitor Type**: `HTTP(s)` hoặc `Web`.
   - **Friendly Name**: `Chronos Bot Keep-Alive`.
   - **URL (IP)**: Điền URL Render của bot (ví dụ: `https://ten-ung-dung-cua-ban.onrender.com`).
   - **Monitoring Interval**: Chọn `5 minutes` hoặc `10 minutes` (Dưới 15 phút là được).
3. Lưu lại. Cứ mỗi 5 phút, UptimeRobot sẽ gửi 1 request ping đến cổng Web của bot. Điều này sẽ khiến Render hiểu là dịch vụ đang hoạt động liên tục và **không bao giờ cho bot đi ngủ**.

---

## 💾 Hướng Dẫn Sao Lưu (Backup) Database SQLite

Vì database lưu trữ dữ liệu điểm danh, điểm EXP, vật phẩm đã mua của các thành viên, việc backup định kỳ rất quan trọng.

- **Nếu dùng Docker**: File database được lưu tại ổ cứng VPS theo đường dẫn volume của Docker. Bạn có thể copy file database ra ngoài để backup:
  ```bash
  # Tìm thư mục lưu trữ volume của Docker
  docker volume inspect discord-bot_bot_data
  ```
- **Nếu chạy bằng Systemd**: File database nằm ngay trong thư mục dự án (`chronos_discipline.db`). Bạn chỉ cần tải file này về máy cá nhân hoặc backup qua một script tự động gửi file lên Google Drive / Telegram.
