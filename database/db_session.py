import logging
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import config

logger = logging.getLogger("ChronosBot.Database")

# Tạo engine kết nối bất đồng bộ. SQLite yêu cầu cấu hình đặc biệt cho chế độ multi-threading nếu dùng file
# Nhưng vì chúng ta chạy async một luồng duy nhất nên mặc định đã an toàn.
# echo=False: tắt in log SQL thô. Bạn có thể bật lại bằng True khi cần debug.
engine = create_async_engine(
    config.DATABASE_URL, 
    echo=False,
    future=True
)

# Tạo async_sessionmaker để sinh ra các Session giao tiếp với DB
async_session = async_sessionmaker(
    bind=engine, 
    class_=AsyncSession, 
    expire_on_commit=False
)

@asynccontextmanager
async def get_db_session():
    """
    Context manager bất đồng bộ cung cấp Session làm việc với DB.
    Đảm bảo session luôn được đóng tự động sau khi dùng xong, tránh leak kết nối.
    Sử dụng:
        async with get_db_session() as session:
            # thực hiện truy vấn ở đây
    """
    session = async_session()
    try:
        yield session
    except Exception as e:
        logger.error(f"Database session error: {e}")
        await session.rollback()
        raise
    finally:
        await session.close()

from sqlalchemy import text

async def run_migrations(conn):
    """
    Tự động nâng cấp cấu trúc cơ sở dữ liệu SQLite nếu các cột mới của User chưa tồn tại.
    """
    logger.info("Đang chạy kiểm tra nâng cấp cấu trúc cơ sở dữ liệu (Migrations)...")
    result = await conn.execute(text("PRAGMA table_info(users)"))
    columns = [row[1] for row in result.fetchall()]
    
    if "active_title" not in columns:
        logger.info("Thêm cột active_title vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN active_title VARCHAR(100)"))
    if "custom_title" not in columns:
        logger.info("Thêm cột custom_title vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN custom_title VARCHAR(100)"))
    if "custom_title_expiry" not in columns:
        logger.info("Thêm cột custom_title_expiry vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN custom_title_expiry DATETIME"))
    if "active_color" not in columns:
        logger.info("Thêm cột active_color vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN active_color VARCHAR(50)"))
    if "chameleon_enabled" not in columns:
        logger.info("Thêm cột chameleon_enabled vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN chameleon_enabled BOOLEAN DEFAULT 0 NOT NULL"))
    if "unlocked_student_titles" not in columns:
        logger.info("Thêm cột unlocked_student_titles vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN unlocked_student_titles BOOLEAN DEFAULT 0 NOT NULL"))
    if "x2_expiry" not in columns:
        logger.info("Thêm cột x2_expiry vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN x2_expiry DATETIME"))
    if "last_muzzle_used" not in columns:
        logger.info("Thêm cột last_muzzle_used vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN last_muzzle_used DATETIME"))
    if "shame_expiry" not in columns:
        logger.info("Thêm cột shame_expiry vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN shame_expiry DATETIME"))
        
    should_backfill = False
    if "total_focus_minutes" not in columns:
        logger.info("Thêm cột total_focus_minutes vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN total_focus_minutes INTEGER DEFAULT 0 NOT NULL"))
        should_backfill = True
    if "total_focus_sessions" not in columns:
        logger.info("Thêm cột total_focus_sessions vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN total_focus_sessions INTEGER DEFAULT 0 NOT NULL"))
        should_backfill = True
    if "week_focus_minutes" not in columns:
        logger.info("Thêm cột week_focus_minutes vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN week_focus_minutes INTEGER DEFAULT 0 NOT NULL"))
        should_backfill = True
    if "focus_minutes_work" not in columns:
        logger.info("Thêm cột focus_minutes_work vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN focus_minutes_work INTEGER DEFAULT 0 NOT NULL"))
        should_backfill = True
    if "focus_minutes_study" not in columns:
        logger.info("Thêm cột focus_minutes_study vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN focus_minutes_study INTEGER DEFAULT 0 NOT NULL"))
        should_backfill = True
    if "focus_minutes_entertainment" not in columns:
        logger.info("Thêm cột focus_minutes_entertainment vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN focus_minutes_entertainment INTEGER DEFAULT 0 NOT NULL"))
        should_backfill = True
    if "focus_minutes_other" not in columns:
        logger.info("Thêm cột focus_minutes_other vào bảng users...")
        await conn.execute(text("ALTER TABLE users ADD COLUMN focus_minutes_other INTEGER DEFAULT 0 NOT NULL"))
        should_backfill = True

    if should_backfill:
        logger.info("Tiến hành backfill dữ liệu lịch sử tập trung từ các phiên cũ vào các cột mới...")
        try:
            # Lấy toàn bộ các FocusSession có status = 'completed' và task tương ứng (nếu có)
            sessions_query = await conn.execute(text("""
                SELECT fs.user_id, fs.actual_duration, t.task_type
                FROM focus_sessions fs
                LEFT JOIN tasks t ON fs.task_id = t.task_id
                WHERE fs.status = 'completed'
            """))
            rows = sessions_query.fetchall()
            
            user_stats = {}
            for uid, duration, task_type in rows:
                if uid not in user_stats:
                    user_stats[uid] = {
                        "total_mins": 0,
                        "total_sessions": 0,
                        "work_mins": 0,
                        "study_mins": 0,
                        "ent_mins": 0,
                        "other_mins": 0
                    }
                user_stats[uid]["total_mins"] += duration
                user_stats[uid]["total_sessions"] += 1
                
                t_type = task_type.strip().lower() if task_type else "khác"
                if t_type == "làm việc":
                    user_stats[uid]["work_mins"] += duration
                elif t_type == "học tập":
                    user_stats[uid]["study_mins"] += duration
                elif t_type == "giải trí":
                    user_stats[uid]["ent_mins"] += duration
                else:
                    user_stats[uid]["other_mins"] += duration
            
            for uid, stats in user_stats.items():
                await conn.execute(text("""
                    UPDATE users
                    SET total_focus_minutes = :total_mins,
                        total_focus_sessions = :total_sessions,
                        week_focus_minutes = :total_mins,
                        focus_minutes_work = :work_mins,
                        focus_minutes_study = :study_mins,
                        focus_minutes_entertainment = :ent_mins,
                        focus_minutes_other = :other_mins
                    WHERE user_id = :uid
                """), {
                    "total_mins": stats["total_mins"],
                    "total_sessions": stats["total_sessions"],
                    "work_mins": stats["work_mins"],
                    "study_mins": stats["study_mins"],
                    "ent_mins": stats["ent_mins"],
                    "other_mins": stats["other_mins"],
                    "uid": uid
                })
            logger.info(f"Đã backfill dữ liệu thành công cho {len(user_stats)} người dùng!")
        except Exception as e:
            logger.error(f"Lỗi xảy ra trong quá trình backfill di trú: {e}")

    # Kiểm tra cột mới trong bảng tasks
    result_task = await conn.execute(text("PRAGMA table_info(tasks)"))
    task_columns = [row[1] for row in result_task.fetchall()]
    if "task_type" not in task_columns:
        logger.info("Thêm cột task_type vào bảng tasks...")
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN task_type VARCHAR(50) DEFAULT 'làm việc'"))
    if "is_recurring" not in task_columns:
        logger.info("Thêm cột is_recurring vào bảng tasks...")
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN is_recurring BOOLEAN DEFAULT 1 NOT NULL"))
    if "days_of_week" not in task_columns:
        logger.info("Thêm cột days_of_week vào bảng tasks...")
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN days_of_week VARCHAR(50)"))
    if "start_date" not in task_columns:
        logger.info("Thêm cột start_date vào bảng tasks...")
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN start_date DATE DEFAULT CURRENT_DATE"))
        
    logger.info("Đã hoàn thành kiểm tra cấu trúc cơ sở dữ liệu!")
async def seed_items():
    """
    Nạp dữ liệu danh sách vật phẩm Grand Mall vào bảng items, cập nhật hoặc thêm mới nếu cần.
    """
    from database.models import Item
    from sqlalchemy import select

    # Danh sách vật phẩm mới trong Grand Mall
    default_items = [
        # Phân khu 1: Cosmetics
        Item(
            item_id="title_student", 
            name="Gói Title Sinh Viên", 
            description="Mở khóa các danh hiệu hài hước để đeo trên Profile: 'Đẹp trai có gì sai', 'Hôm nay tôi buồn', 'Chúa tể chạy Deadline', 'Kẻ thù của giường ngủ'.", 
            price=25, 
            item_type="cosmetic", 
            is_gacha_only=False, 
            gacha_weight=10
        ),
        Item(
            item_id="name_color", 
            name="Bảng Tên Dạ Quang", 
            description="Quyền đổi màu tên của bạn trên server thành các màu đặc biệt: Neon Pink, Hacker Green, Blood Red.", 
            price=60, 
            item_type="cosmetic", 
            is_gacha_only=False, 
            gacha_weight=10
        ),
        Item(
            item_id="title_custom", 
            name="Gói Thẻ Tùy Biến", 
            description="Tự đặt danh hiệu riêng của bạn (Ví dụ: 'Vợ anh A', 'CEO Tương lai'). Duy trì 30 ngày hằng lượt sử dụng.", 
            price=150, 
            item_type="cosmetic", 
            is_gacha_only=False, 
            gacha_weight=5
        ),
        Item(
            item_id="chameleon", 
            name="Hiệu Ứng Tắc Kè Hoa", 
            description="Tự động đổi màu tên của bạn trên server 1 tiếng/lần cực kỳ nổi bật.", 
            price=300, 
            item_type="cosmetic", 
            is_gacha_only=False, 
            gacha_weight=1
        ),
        
        # Phân khu 2: Utility
        Item(
            item_id="coffee", 
            name="☕ Cà Phê Đen Đá", 
            description="Cộng thêm 30 phút vào ca Focus hiện tại mà không làm ngắt quãng sự tập trung.", 
            price=15, 
            item_type="utility", 
            is_gacha_only=False, 
            gacha_weight=30
        ),
        Item(
            item_id="rest_token", 
            name="🛡️ Thẻ Nghỉ Phép", 
            description="Tự động sử dụng khi vắng mặt để giữ chuỗi streak kỷ luật của bạn.", 
            price=50, 
            item_type="survival", 
            is_gacha_only=False, 
            gacha_weight=10
        ),
        Item(
            item_id="x2_speed", 
            name="⏳ Thẻ X2 Tốc Độ", 
            description="Nhân đôi lượng EXP và Token nhận được từ việc tập trung trong vòng 2 tiếng tiếp theo.", 
            price=75, 
            item_type="utility", 
            is_gacha_only=False, 
            gacha_weight=15
        ),
        Item(
            item_id="eraser", 
            name="🧼 Cục Tẩy Khổng Lồ", 
            description="Xóa sạch 1 vết đen (1 lần vi phạm) trong Hồ Sơ Vi Phạm kỷ luật.", 
            price=250, 
            item_type="survival", 
            is_gacha_only=False, 
            gacha_weight=2
        ),

        # Phân khu 3: Social Interactions
        Item(
            item_id="loudspeaker", 
            name="📢 Loa Phường", 
            description="Ghim 1 tin nhắn bất kỳ của bạn lên kênh Chat Chung trong vòng 1 tiếng.", 
            price=40, 
            item_type="utility", 
            is_gacha_only=False, 
            gacha_weight=10
        ),
        Item(
            item_id="music_lyrical", 
            name="🎶 Nhạc Trữ Tình", 
            description="Khi bạn vào kênh Voice, bot sẽ vào theo và bật đoạn nhạc 5 giây siêu ngầu rồi tự thoát.", 
            price=120, 
            item_type="utility", 
            is_gacha_only=False, 
            gacha_weight=5
        ),
        Item(
            item_id="summon_card", 
            name="🥊 Thẻ Triệu Hồi", 
            description="Kích hoạt lệnh /summon để liên tục ping bạn bè 3 lần bắt họ vào phòng Focus.", 
            price=30, 
            item_type="utility", 
            is_gacha_only=False, 
            gacha_weight=15
        ),
        Item(
            item_id="muzzle_card", 
            name="🔇 Thẻ 'Khóa Mõm'", 
            description="Mute (Tắt mic) một người bạn bất kỳ trong phòng voice trong đúng 60 giây. (Giới hạn sử dụng 1 lần/ngày).", 
            price=80, 
            item_type="utility", 
            is_gacha_only=False, 
            gacha_weight=5
        )
    ]
    async with get_db_session() as session:
        # 1. Đồng bộ danh sách hàng hóa
        valid_ids = [item.item_id for item in default_items]
        
        # Xóa các vật phẩm cũ đã lỗi thời
        existing_items_res = await session.execute(select(Item))
        all_existing_items = existing_items_res.scalars().all()
        for existing_item in all_existing_items:
            if existing_item.item_id not in valid_ids:
                await session.delete(existing_item)
        
        await session.flush()
        
        # Thêm mới hoặc cập nhật các thuộc tính vật phẩm
        for item in default_items:
            existing_res = await session.execute(select(Item).where(Item.item_id == item.item_id))
            existing = existing_res.scalar_one_or_none()
            if not existing:
                session.add(item)
            else:
                existing.name = item.name
                existing.description = item.description
                existing.price = item.price
                existing.item_type = item.item_type
                existing.is_gacha_only = item.is_gacha_only
                existing.gacha_weight = item.gacha_weight
        
        await session.commit()
        logger.info("Đã đồng bộ danh mục vật phẩm Đại Siêu Thị (Grand Mall) thành công!")

async def init_db():
    """
    Hàm khởi tạo cơ sở dữ liệu.
    Nó sẽ tự động tạo ra tất cả các bảng dựa trên models nếu chúng chưa tồn tại.
    """
    from database.models import Base
    logger.info("Đang khởi tạo cấu trúc cơ sở dữ liệu...")
    async with engine.begin() as conn:
        # Chạy đồng bộ hàm tạo bảng trong môi trường bất đồng bộ
        await conn.run_sync(Base.metadata.create_all)
        # Chạy migrations để bổ sung cột nếu bảng đã tồn tại
        await run_migrations(conn)
    logger.info("Cấu trúc cơ sở dữ liệu đã sẵn sàng!")
    
    # Nạp dữ liệu mẫu
    await seed_items()
