import logging
import discord
from discord.ext import commands
import config

logger = logging.getLogger("ChronosBot.DynamicVoice")

class DynamicVoice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Lưu trữ danh sách các ID kênh thoại tạm thời để quản lý và dọn dẹp
        self.temp_channels = set()

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("DynamicVoice Cog đã sẵn sàng.")
        # Tự động quét và dọn dẹp các phòng thoại Focus trống bị sót từ phiên chạy trước (nếu bot bị crash/restart)
        await self.cleanup_orphaned_channels()

    async def cleanup_orphaned_channels(self):
        """
        Dọn dẹp các phòng thoại '🎧 Focus:' mồ côi không có thành viên nào.
        """
        logger.info("Đang quét dọn các kênh thoại Focus trống còn sót lại...")
        deleted_count = 0
        for guild in self.bot.guilds:
            for vc in guild.voice_channels:
                if vc.name.startswith("🎧 Focus:") and len(vc.members) == 0:
                    try:
                        await vc.delete(reason="Dọn dẹp phòng trống khi khởi tạo bot")
                        deleted_count += 1
                    except discord.Forbidden:
                        logger.warning(f"Thiếu quyền xóa kênh {vc.name} (ID: {vc.id})")
                    except Exception as e:
                        logger.error(f"Lỗi khi dọn dẹp kênh {vc.name} (ID: {vc.id}): {e}")
        if deleted_count > 0:
            logger.info(f"Đã dọn dẹp {deleted_count} phòng thoại mồ côi thành công.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """
        Lắng nghe sự kiện chuyển đổi trạng thái Voice của thành viên.
        """
        if member.bot:
            return

        # 1. HÀNH ĐỘNG: Vào kênh thoại trigger (➕ [Bấm vào để Focus])
        if after.channel is not None and after.channel.id == config.FOCUS_TRIGGER_CHANNEL_ID:
            trigger_channel = after.channel
            guild = trigger_channel.guild
            category = trigger_channel.category

            logger.info(f"Thành viên {member.display_name} kích hoạt tạo phòng voice động.")

            # Thiết lập quyền hạn cho phòng voice mới:
            # - Cho phép Mod/Admin và bản thân User có quyền tối đa.
            # - Cho phép thành viên khác kết nối (để có thể cùng làm việc nếu muốn) nhưng tùy chỉnh sau.
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(connect=True, view_channel=True),
                member: discord.PermissionOverwrite(
                    connect=True, 
                    speak=True, 
                    view_channel=True,
                    mute_members=False,  # Tránh lạm dụng quyền admin voice
                    deafen_members=False,
                    move_members=False
                )
            }

            try:
                # Tạo kênh voice tạm thời
                channel_name = f"🎧 Focus: {member.display_name}"
                new_channel = await guild.create_voice_channel(
                    name=channel_name,
                    category=category,
                    overwrites=overwrites,
                    reason=f"Phòng Focus tự động tạo cho {member.name}"
                )

                # Lưu ID vào danh sách theo dõi
                self.temp_channels.add(new_channel.id)
                logger.info(f"Đã tạo kênh voice '{channel_name}' (ID: {new_channel.id}).")

                # Di chuyển User sang phòng mới tạo
                await member.move_to(new_channel, reason="Di chuyển sang phòng Focus riêng biệt")
                logger.info(f"Đã di chuyển {member.display_name} vào phòng Focus riêng.")

            except discord.Forbidden:
                logger.error("Bot không có quyền tạo kênh voice hoặc di chuyển thành viên.")
                # Gửi thông báo ẩn nếu có thể
                try:
                    await member.send("❌ Bot thiếu quyền tạo kênh voice hoặc di chuyển bạn sang phòng mới. Vui lòng báo Admin.")
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Lỗi khi xử lý tạo phòng voice động: {e}")

        # 2. HÀNH ĐỘNG: Rời phòng hoặc chuyển phòng (Kiểm tra và dọn phòng trống)
        if before.channel is not None:
            old_channel = before.channel
            
            # Kiểm tra nếu phòng vừa rời thuộc danh sách phòng tạm thời do bot tạo ra
            # Hoặc tên phòng bắt đầu bằng "🎧 Focus:" (trong trường hợp bot restart mất ram cache)
            if old_channel.id in self.temp_channels or old_channel.name.startswith("🎧 Focus:"):
                # Nếu phòng không còn ai học/làm việc
                if len(old_channel.members) == 0:
                    try:
                        await old_channel.delete(reason="Phòng Focus động đã trống thành viên")
                        self.temp_channels.discard(old_channel.id)
                        logger.info(f"Đã xóa kênh voice trống '{old_channel.name}' (ID: {old_channel.id}).")
                    except discord.NotFound:
                        self.temp_channels.discard(old_channel.id)
                    except discord.Forbidden:
                        logger.warning(f"Thiếu quyền xóa kênh {old_channel.name} khi dọn dẹp")
                    except Exception as e:
                        logger.error(f"Lỗi khi xóa kênh voice trống: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(DynamicVoice(bot))
