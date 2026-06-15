import os
from dotenv import load_dotenv

# Tải các biến môi trường từ file .env nếu có
load_dotenv()

# TOKEN BOT DISCORD
# Khuyến khích đưa token vào file .env dưới dạng DISCORD_TOKEN=...
BOT_TOKEN = os.getenv("DISCORD_TOKEN", "")

# CƠ SỞ DỮ LIỆU
# SQLite cục bộ để test, sử dụng aiosqlite làm driver async
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///chronos_discipline.db")

# CẤU HÌNH KÊNH & ROLE DISCORD (Đồng bộ từ database.json cũ)
PHONG_LAM_VIEC_ID = int(os.getenv("PHONG_LAM_VIEC_ID", 1515359289945096315))
PHONG_HOC_TAP_ID = int(os.getenv("PHONG_HOC_TAP_ID", 1515359222576185595))
KENH_THONG_BAO_ID = int(os.getenv("KENH_THONG_BAO_ID", 1515356648590413855))
KENH_LOG_ID = int(os.getenv("KENH_LOG_ID", 0))
KENH_CHAO_MUNG_ID = int(os.getenv("KENH_CHAO_MUNG_ID", 0))
KENH_BXH_ID = int(os.getenv("KENH_BXH_ID", 1515356648590413856))

ROLE_ADMIN_ID = int(os.getenv("ROLE_ADMIN_ID", 0))
ROLE_MOD_ID = int(os.getenv("ROLE_MOD_ID", 1515359957598339263))
ROLE_MEMBER_ID = int(os.getenv("ROLE_MEMBER_ID", 0))

# ID Kênh Voice cố định dùng để click tạo phòng thoại động (➕ [Bấm vào để Focus])
# Bạn hãy thay thế ID này bằng ID kênh voice thực tế trên server của bạn
FOCUS_TRIGGER_CHANNEL_ID = int(os.getenv("FOCUS_TRIGGER_CHANNEL_ID", 1515359222576185595)) 

