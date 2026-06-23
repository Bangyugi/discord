from datetime import datetime, time, date
from typing import List, Optional
from sqlalchemy import BigInteger, ForeignKey, String, Text, Integer, Float, Boolean, Time, Date, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Khởi tạo DeclarativeBase chuẩn SQLAlchemy 2.0
class Base(DeclarativeBase):
    pass

class User(Base):
    """
    Thực thể User đại diện cho các thành viên Discord sử dụng hệ thống.
    """
    __tablename__ = "users"

    # Discord User ID là số nguyên 64-bit (sử dụng BigInteger để tương thích PostgreSQL sau này)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    
    # Chỉ số tích lũy & Cấp độ
    exp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    level: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    
    # Tiền tệ trong bot
    token_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    
    # Chuỗi kỷ luật (Streak)
    current_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    
    # Ngày tập trung cuối cùng (Dùng để tính streak chính xác không phụ thuộc múi giờ máy chủ)
    last_focus_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    
    # Thống kê tích lũy thời gian Focus
    total_focus_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_focus_sessions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    week_focus_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    
    # Phân bổ nội dung tập trung tích lũy
    focus_minutes_work: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    focus_minutes_study: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    focus_minutes_entertainment: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    focus_minutes_other: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    
    # Các trường mở rộng cho hệ thống Economy & Grand Mall
    active_title: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    custom_title: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    custom_title_expiry: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    active_color: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    chameleon_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    unlocked_student_titles: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    x2_expiry: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_muzzle_used: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Thời điểm tạo tài khoản trên hệ thống bot
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)

    # Các mối quan hệ (Relationships)
    tasks: Mapped[List["Task"]] = relationship("Task", back_populates="user", cascade="all, delete-orphan")
    focus_sessions: Mapped[List["FocusSession"]] = relationship("FocusSession", back_populates="user", cascade="all, delete-orphan")
    inventory: Mapped[List["Inventory"]] = relationship("Inventory", back_populates="user", cascade="all, delete-orphan")
    violations: Mapped[List["ViolationLog"]] = relationship("ViolationLog", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<User id={self.user_id} lvl={self.level} xp={self.exp} tokens={self.token_balance}>"


class Task(Base):
    """
    Thực thể Task đại diện cho mục tiêu học tập/làm việc đã đăng ký của User.
    """
    __tablename__ = "tasks"

    task_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    
    # Thông tin task
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    
    # Giờ bắt đầu (Ví dụ: 20:00)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    
    # Thời lượng (tính bằng phút)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    
    # Ngày bắt đầu (Ví dụ: 14/06/2026)
    start_date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    
    # Loại task: "giải trí", "làm việc", "học tập"
    task_type: Mapped[str] = mapped_column(String(50), nullable=False, default="làm việc")
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)

    # Các mối quan hệ
    user: Mapped["User"] = relationship("User", back_populates="tasks")
    focus_sessions: Mapped[List["FocusSession"]] = relationship("FocusSession", back_populates="task", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Task id={self.task_id} title='{self.title}' user={self.user_id}>"


class FocusSession(Base):
    """
    Thực thể FocusSession lưu lại lịch sử chi tiết mỗi lần User tham gia học/làm việc tập trung.
    """
    __tablename__ = "focus_sessions"

    session_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("tasks.task_id", ondelete="SET NULL"), nullable=True)
    
    # Thời gian bắt đầu và kết thúc thực tế
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    # Thời lượng thực tế thực hiện được (tính bằng phút)
    actual_duration: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    
    # Trạng thái session: "pending" (đang chờ/đang chạy), "completed" (thành công), "failed" (vi phạm/thất bại)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)

    # Các mối quan hệ
    user: Mapped["User"] = relationship("User", back_populates="focus_sessions")
    task: Mapped[Optional["Task"]] = relationship("Task", back_populates="focus_sessions")

    def __repr__(self) -> str:
        return f"<FocusSession id={self.session_id} user={self.user_id} status={self.status}>"


class Item(Base):
    """
    Thực thể Item lưu danh mục vật phẩm có sẵn trong cửa hàng (Shop) hoặc vòng quay Gacha.
    """
    __tablename__ = "items"

    # Mã định danh vật phẩm (Ví dụ: "rest_token", "violation_remover", "role_color_gold")
    item_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Giá mua vật phẩm bằng Token
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    
    # Phân loại: "cosmetic" (Role, tên màu), "utility" (âm thanh chào mừng), "survival" (Thẻ nghỉ phép, Thẻ xóa vi phạm)
    item_type: Mapped[str] = mapped_column(String(50), nullable=False)
    
    # Có thể mua trực tiếp trong shop không hay chỉ có thể quay Gacha ra
    is_gacha_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    
    # Trọng số rơi trong Gacha (Dùng để tính tỷ lệ % drop rate)
    gacha_weight: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)

    # Mối quan hệ túi đồ
    inventories: Mapped[List["Inventory"]] = relationship("Inventory", back_populates="item", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Item id='{self.item_id}' name='{self.name}' price={self.price}>"


class Inventory(Base):
    """
    Thực thể Inventory lưu giữ túi đồ thực tế của từng User (Số lượng vật phẩm sở hữu).
    """
    __tablename__ = "inventories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[str] = mapped_column(String(50), ForeignKey("items.item_id", ondelete="CASCADE"), nullable=False)
    
    # Số lượng vật phẩm đang giữ
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    
    # Ngày mua/sở hữu vật phẩm
    purchased_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)

    # Các mối quan hệ
    user: Mapped["User"] = relationship("User", back_populates="inventory")
    item: Mapped["Item"] = relationship("Item", back_populates="inventories")

    def __repr__(self) -> str:
        return f"<Inventory user={self.user_id} item='{self.item_id}' qty={self.quantity}>"


class ViolationLog(Base):
    """
    Thực thể ViolationLog lưu giữ lịch sử vi phạm kỷ luật của User và giải trình lý do (Lưu vĩnh viễn).
    """
    __tablename__ = "violation_logs"

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    
    # Nội dung lý do vi phạm hoặc giải trình của người dùng
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)

    # Mối quan hệ với User
    user: Mapped["User"] = relationship("User", back_populates="violations")

    def __repr__(self) -> str:
        return f"<ViolationLog id={self.log_id} user={self.user_id} date={self.created_at}>"


class TaskInvite(Base):
    """
    Thực thể TaskInvite lưu giữ thông tin lời mời làm việc cùng.
    """
    __tablename__ = "task_invites"

    invite_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False)
    invitee_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False) # "pending", "accepted", "declined"
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True) # ID of the DM message containing the invite
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)

    # Các mối quan hệ
    task: Mapped["Task"] = relationship("Task")

    def __repr__(self) -> str:
        return f"<TaskInvite id={self.invite_id} task_id={self.task_id} invitee={self.invitee_id} status={self.status}>"

