import os
import shutil
import json
import asyncio
import logging
from datetime import datetime

# Cấu hình logging cơ bản để hiển thị tiến trình
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ResetDB")

async def reset_database():
    import config
    from database.db_session import init_db

    # 1. Tạo thư mục backup
    backup_dir = "backups"
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)
        logger.info(f"Đã tạo thư mục sao lưu: {backup_dir}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 2. Xác định file SQLite DB cần sao lưu và xóa
    db_url = config.DATABASE_URL
    db_filename = None
    if db_url.startswith("sqlite+aiosqlite:///"):
        db_filename = db_url.replace("sqlite+aiosqlite:///", "")
    elif db_url.startswith("sqlite:///"):
        db_filename = db_url.replace("sqlite:///", "")

    if db_filename and os.path.exists(db_filename):
        # Sao lưu
        backup_db_path = os.path.join(backup_dir, f"{db_filename}_{timestamp}.bak")
        try:
            shutil.copy2(db_filename, backup_db_path)
            logger.info(f"Đã sao lưu database thành công: {db_filename} -> {backup_db_path}")
        except Exception as e:
            logger.error(f"Lỗi khi sao lưu database: {e}")
            raise e

        # Xóa file chính và các file tạm thời của SQLite
        file_deleted = True
        for suffix in ["", "-wal", "-shm", "-journal"]:
            temp_file = db_filename + suffix
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                    logger.info(f"Đã xóa tệp dữ liệu SQLite cũ: {temp_file}")
                except Exception as e:
                    logger.warning(f"Không thể xóa tệp {temp_file} (có thể đang bị khóa): {e}")
                    if suffix == "":
                        file_deleted = False

        if not file_deleted and os.path.exists(db_filename):
            # Nếu không thể xóa file (ví dụ do đang chạy bot), ta sẽ xóa các bản ghi trong DB bằng SQLAlchemy
            from database.db_session import get_db_session
            from database.models import User, Task, FocusSession, Inventory, ViolationLog, TaskInvite, Item
            from sqlalchemy import delete
            
            logger.info("Database đang bị khóa bởi tiến trình khác. Tiến hành xóa dữ liệu trực tiếp trong các bảng...")
            async with get_db_session() as session:
                try:
                    await session.execute(delete(TaskInvite))
                    await session.execute(delete(ViolationLog))
                    await session.execute(delete(Inventory))
                    await session.execute(delete(FocusSession))
                    await session.execute(delete(Task))
                    await session.execute(delete(User))
                    await session.execute(delete(Item))
                    await session.commit()
                    logger.info("Đã xóa toàn bộ dữ liệu trong các bảng thành công!")
                except Exception as e:
                    logger.error(f"Lỗi khi xóa dữ liệu trực tiếp từ các bảng: {e}")
                    await session.rollback()
                    raise e
    else:
        logger.info("Không tìm thấy tệp database SQLite cũ hoặc cấu hình URL không phải SQLite cục bộ.")

    # 3. Sao lưu và làm sạch database.json (nếu tồn tại)
    database_json_path = "database.json"
    if os.path.exists(database_json_path):
        backup_json_path = os.path.join(backup_dir, f"{database_json_path}_{timestamp}.bak")
        try:
            shutil.copy2(database_json_path, backup_json_path)
            logger.info(f"Đã sao lưu database.json thành công: {database_json_path} -> {backup_json_path}")
            
            # Đọc và làm sạch users
            with open(database_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            data["users"] = {}
            
            with open(database_json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            logger.info("Đã làm sạch dữ liệu 'users' trong database.json nhưng vẫn giữ nguyên cấu hình.")
        except Exception as e:
            logger.error(f"Lỗi khi xử lý database.json: {e}")

    # 4. Sao lưu và xóa leaderboard_state.json (nếu tồn tại)
    leaderboard_state_path = "database/leaderboard_state.json"
    if os.path.exists(leaderboard_state_path):
        backup_state_path = os.path.join(backup_dir, f"leaderboard_state.json_{timestamp}.bak")
        try:
            shutil.copy2(leaderboard_state_path, backup_state_path)
            logger.info(f"Đã sao lưu leaderboard_state.json thành công: {leaderboard_state_path} -> {backup_state_path}")
            os.remove(leaderboard_state_path)
            logger.info(f"Đã xóa file trạng thái bảng xếp hạng cũ: {leaderboard_state_path}")
        except Exception as e:
            logger.error(f"Lỗi khi xử lý leaderboard_state.json: {e}")

    # 5. Khởi tạo lại database mới
    logger.info("Bắt đầu khởi tạo lại database SQLite mới và nạp danh mục vật phẩm...")
    try:
        await init_db()
        logger.info("Đã khởi tạo thành công database mới sạch sẽ!")
    except Exception as e:
        logger.error(f"Lỗi khi khởi tạo database mới: {e}")
        raise e

if __name__ == "__main__":
    asyncio.run(reset_database())
