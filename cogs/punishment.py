import logging
import math
import datetime
import discord
from discord.ext import commands, tasks
from sqlalchemy import select
import config
from database.db_session import get_db_session
from database.models import User, ViolationLog

logger = logging.getLogger("ChronosBot.Punishment")

class Punishment(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("Punishment Cog đã sẵn sàng.")

    def calculate_level(self, xp: int) -> int:
        """Tính toán cấp độ từ XP hiện tại."""
        if xp <= 0:
            return 1
        return int(math.sqrt(xp) / 10) + 1

    async def apply_punishment(self, user_id: int, guild_id: int, reason: str):
        """
        Áp dụng hình phạt kỷ luật đối với người dùng vi phạm:
        1. Trừ 50 EXP (không để dưới 0), cập nhật lại cấp độ nếu rớt cấp.
        2. Reset chuỗi Streak hiện tại về 0.
        3. Ghi Violation Log vĩnh viễn vào DB.
        """
        logger.info(f"Đang áp dụng hình phạt cho User ID {user_id}. Lý do: {reason}")
        now = datetime.datetime.now()

        async with get_db_session() as session:
            # 1. Tìm kiếm hoặc khởi tạo User trong DB
            user_res = await session.execute(select(User).filter_by(user_id=user_id))
            user = user_res.scalar_one_or_none()
            
            if not user:
                user = User(user_id=user_id, exp=0, level=1, token_balance=0)
                session.add(user)
                await session.flush()

            old_level = user.level
            
            # Trừ 50 XP (Giới hạn tối thiểu là 0)
            user.exp = max(0, user.exp - 50)
            user.level = self.calculate_level(user.exp)
            
            # Reset streak về 0
            user.current_streak = 0

            # 2. Lưu lý do vi phạm vào Log
            log_entry = ViolationLog(
                user_id=user_id,
                reason=reason,
                created_at=now
            )
            session.add(log_entry)
            await session.commit()

        # 3. Thực thi thông báo và xử lý danh hiệu
        guild = self.bot.get_guild(guild_id)
        if guild:
            member = guild.get_member(user_id)
            if member:
                # Gửi thông báo DM cho người dùng
                try:
                    embed_dm = discord.Embed(
                        title="⚠️ CẢNH BÁO VI PHẠM KỶ LUẬT ⚠️",
                        description=(
                            f"Bạn đã bị hệ thống phạt do vi phạm kỷ luật thời gian.\n\n"
                            f"• **Lý do:** {reason}\n"
                            f"• **Hình phạt:** Trừ `50 EXP`, đặt chuỗi Streak về `0` ngày."
                        ),
                        color=discord.Color.red(),
                        timestamp=now
                    )
                    await member.send(embed=embed_dm)
                except discord.Forbidden:
                    logger.warning(f"Không thể gửi tin nhắn DM cảnh báo cho {member.name}")

                # Tự động tháo bỏ các Role danh hiệu khác nếu bị rớt cấp
                if user.level < old_level:
                    logger.info(f"Thành viên {member.name} bị rớt cấp từ {old_level} xuống {user.level}.")
                    # (Logic tháo role cấp độ cụ thể có thể bổ sung tùy thuộc vào ID role cấp độ của server bạn)

async def setup(bot: commands.Bot):
    await bot.add_cog(Punishment(bot))
