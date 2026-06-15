import logging
import math
import datetime
import discord
from discord.ext import commands
from discord import app_commands
from sqlalchemy import select
from database.db_session import get_db_session
from database.models import User, FocusSession, Task, ViolationLog

logger = logging.getLogger("ChronosBot.Profile")

class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Đăng ký User Context Menu "Xem Hồ Sơ"
        # Xuất hiện khi chuột phải vào member -> Chọn Apps -> Xem Hồ Sơ
        self.ctx_menu = app_commands.ContextMenu(
            name="Xem Hồ Sơ",
            callback=self.view_profile_callback
        )
        self.bot.tree.add_command(self.ctx_menu)

    def cog_unload(self):
        # Gỡ bỏ Context Menu khi Cog bị unload để tránh trùng lặp lệnh
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("Profile Cog đã sẵn sàng.")

async def get_profile_embed(member: discord.Member) -> discord.Embed:
    """
    Tạo embed hiển thị thẻ hồ sơ năng suất của thành viên.
    """
    user_id = member.id
    now = datetime.datetime.now()

    async with get_db_session() as session:
        # 1. Truy vấn thông tin user
        user_res = await session.execute(select(User).filter_by(user_id=user_id))
        user = user_res.scalar_one_or_none()
        
        # 2. Truy vấn danh sách FocusSession hoàn thành kèm Task tương ứng
        fs_res = await session.execute(
            select(FocusSession, Task)
            .outerjoin(Task, FocusSession.task_id == Task.task_id)
            .where(FocusSession.user_id == user_id, FocusSession.status == "completed")
        )
        completed_sessions = fs_res.all()
        
        # 3. Truy vấn tổng số lần vi phạm kỷ luật
        violation_res = await session.execute(
            select(ViolationLog).where(ViolationLog.user_id == user_id)
        )
        violations = violation_res.scalars().all()

    # --- TÍNH TOÁN CÁC CHỈ SỐ ---
    
    # Chỉ số cơ bản
    exp = user.exp if user else 0
    level = user.level if user else 1
    tokens = user.token_balance if user else 0
    current_streak = user.current_streak if user else 0
    max_streak = user.max_streak if user else 0
    violation_count = len(violations)

    # Tính toán thanh tiến trình cấp độ (Progress Bar)
    next_level_xp = (level * 10) ** 2
    prev_level_xp = ((level - 1) * 10) ** 2 if level > 1 else 0
    segment_xp = next_level_xp - prev_level_xp
    user_segment_xp = exp - prev_level_xp
    
    ratio = 0.0
    if segment_xp > 0:
        ratio = max(0.0, min(1.0, user_segment_xp / segment_xp))
    
    progress = int(ratio * 10)
    progress_bar = "🟩" * progress + "⬜" * (10 - progress)

    # Tính toán thời gian Focus tích lũy & phân loại mục tiêu
    total_focus_minutes = 0
    code_minutes = 0
    study_minutes = 0
    work_minutes = 0
    
    # Danh mục từ khóa phân loại
    code_keywords = ["code", "python", "dev", "java", "c++", "html", "programming", "lập trình", "git", "coding", "web", "rust"]
    study_keywords = ["english", "ielts", "anh", "tieng", "japan", "korean", "học", "study", "read", "sách", "book", "toán", "math", "class"]

    for fs, task in completed_sessions:
        duration = fs.actual_duration
        total_focus_minutes += duration
        
        if task:
            title_lower = task.title.lower()
            # Phân loại danh mục
            if any(keyword in title_lower for keyword in code_keywords):
                code_minutes += duration
            elif any(keyword in title_lower for keyword in study_keywords):
                study_minutes += duration
            else:
                work_minutes += duration
        else:
            work_minutes += duration

    # Đổi phút sang Giờ & Phút
    total_hours, total_mins = total_focus_minutes // 60, total_focus_minutes % 60
    code_hours, code_mins = code_minutes // 60, code_minutes % 60
    study_hours, study_mins = study_minutes // 60, study_minutes % 60
    work_hours, work_mins = work_minutes // 60, work_minutes % 60

    # --- DỰNG EMBED PROFILE ---
    active_title = user.active_title if (user and user.active_title) else "Chưa trang bị"
    active_color = user.active_color if (user and user.active_color) else None
    
    # Mapping màu sắc hiển thị cho embed
    color_map = {
        "Neon Pink": discord.Color.from_rgb(255, 20, 147),
        "Hacker Green": discord.Color.from_rgb(57, 255, 20),
        "Blood Red": discord.Color.from_rgb(255, 0, 0),
        "Chameleon": discord.Color.from_rgb(241, 196, 15)  # Màu vàng hoàng kim dịch chuyển
    }
    embed_color = color_map.get(active_color, discord.Color.purple())

    active_effects = []
    if user and user.x2_expiry and now < user.x2_expiry:
        rem_sec = (user.x2_expiry - now).total_seconds()
        rem_hours, rem_mins = int(rem_sec // 3600), int((rem_sec % 3600) // 60)
        active_effects.append(f"⚡ **X2 Tốc Độ** ({rem_hours}g {rem_mins}p)")
    if user and user.chameleon_enabled:
        active_effects.append("🌈 **Tắc Kè Hoa**")
    
    effects_str = ", ".join(active_effects) if active_effects else "Không có"

    embed = discord.Embed(
        title=f"📊 HỒ SƠ NĂNG SUẤT: {member.display_name}",
        description=(
            f"Thành viên của **Chronos Discipline System**\n"
            f"🎭 **Danh hiệu:** `{active_title}`\n"
            f"✨ **Hiệu ứng active:** {effects_str}"
        ),
        color=embed_color,
        timestamp=now
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    # Trường 1: Cấp độ
    embed.add_field(
        name="⭐ Cấp Độ & Kinh Nghiệm",
        value=f"• **Cấp {level}**\n• `{exp} / {next_level_xp} XP`\n• {progress_bar} (`{ratio:.1%}`)",
        inline=False
    )

    # Trường 2: Kỷ luật Streak
    embed.add_field(
        name="🔥 Chuỗi Kỷ Luật (Streak)",
        value=f"• Chuỗi hiện tại: **`{current_streak} ngày`**\n• Chuỗi cao nhất: **`{max_streak} ngày`**",
        inline=True
    )

    # Trường 3: Tài sản
    embed.add_field(
        name="🪙 Số Dư Token",
        value=f"• Đang sở hữu: **`{tokens} Tokens`**",
        inline=True
    )

    # Trường 4: Thống kê Focus
    embed.add_field(
        name="⏱️ Tổng Thời Gian Focus",
        value=f"• **`{total_hours} giờ {total_mins} phút`**\n• Đã hoàn thành: **`{len(completed_sessions)} phiên`**",
        inline=False
    )

    # Trường 5: Phân bổ nội dung học/làm việc
    embed.add_field(
        name="📚 Phân Bổ Nội Dung Tập Trung",
        value=(
            f"• 💻 Lập trình (Coding): **`{code_hours}g {code_mins}p`**\n"
            f"• 📖 Học tập & Ngoại ngữ: **`{study_hours}g {study_mins}p`**\n"
            f"• 💼 Công việc & Khác: **`{work_hours}g {work_mins}p`**"
        ),
        inline=False
    )

    # Trường 6: Lịch sử vi phạm
    status_text = "🟢 Tốt" if violation_count == 0 else "🟡 Cảnh báo" if violation_count < 3 else "🔴 Báo động"
    embed.add_field(
        name="⚠️ Lịch Sử Vi Phạm Kỷ Luật",
        value=f"• Số lần vi phạm: **`{violation_count} lần`**\n• Đánh giá Kỷ luật: **{status_text}**",
        inline=False
    )

    embed.set_footer(text="Chronos Bot • Ép buộc kỷ luật, mở khóa tiềm năng!")
    return embed


class Profile(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Đăng ký User Context Menu "Xem Hồ Sơ"
        # Xuất hiện khi chuột phải vào member -> Chọn Apps -> Xem Hồ Sơ
        self.ctx_menu = app_commands.ContextMenu(
            name="Xem Hồ Sơ",
            callback=self.view_profile_callback
        )
        self.bot.tree.add_command(self.ctx_menu)

    def cog_unload(self):
        # Gỡ bỏ Context Menu khi Cog bị unload để tránh trùng lặp lệnh
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("Profile Cog đã sẵn sàng.")

    async def view_profile_callback(self, interaction: discord.Interaction, member: discord.Member):
        """
        Callback xử lý hiển thị thẻ hồ sơ năng suất của thành viên.
        """
        if member.bot:
            await interaction.response.send_message("❌ Không thể xem hồ sơ năng suất của Bot!", ephemeral=True)
            return

        # Defer trước để tránh lỗi timeout 3 giây do truy vấn database nhiều bảng
        await interaction.response.defer()

        embed = await get_profile_embed(member)

        # Trả về kết quả hiển thị cho mọi người xem (gửi tin nhắn công khai)
        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))
