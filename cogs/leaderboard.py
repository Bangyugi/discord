import logging
import datetime
import math
import json
import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
from sqlalchemy import select, func
from database.db_session import get_db_session
from database.models import User, FocusSession
import config
from cogs.profile import get_profile_embed
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger("ChronosBot.Leaderboard")

STATE_FILE = "database/leaderboard_state.json"

def get_last_rewarded_week() -> str:
    """Đọc thông tin tuần cuối cùng đã phát thưởng từ file trạng thái."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("last_rewarded_week", "")
        except Exception as e:
            logger.error(f"Lỗi khi đọc file trạng thái phát thưởng: {e}")
    return ""

def set_last_rewarded_week(week_key: str):
    """Lưu tuần vừa phát thưởng thành công vào file trạng thái."""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_rewarded_week": week_key}, f, ensure_ascii=False, indent=4)
        logger.info(f"Đã lưu trạng thái phát thưởng tuần thành công cho: {week_key}")
    except Exception as e:
        logger.error(f"Lỗi khi lưu trạng thái phát thưởng: {e}")

def format_cell(text, width: int, align_left: bool = True) -> str:
    """Định dạng ô dữ liệu căn lề trong code block để tạo bảng thẳng cột."""
    text_str = str(text)
    if len(text_str) > width:
        return text_str[:width-3] + "..."
    if align_left:
        return text_str.ljust(width)
    else:
        return text_str.rjust(width)

def get_tier_and_title(total_hours: float, overall_rank: int) -> tuple[str, str]:
    """Trả về Bậc xếp hạng (Tier) và Danh hiệu tương ứng dựa trên tổng giờ tích lũy."""
    if total_hours < 50:
        return "Đồng", "Người Lữ Hành Tập Sự"
    elif total_hours < 200:
        return "Bạc", "Học Viên Tận Tụy"
    elif total_hours < 500:
        return "Vàng", "Chuyên Gia Tập Trung"
    elif total_hours <= 1000:
        return "Kim Cương", "Cỗ Máy Kỷ Luật"
    else:
        return "Thách Đấu", "Kẻ Hủy Diệt Deadline"


class LeaderboardDropdown(discord.ui.Select):
    """
    Menu thả xuống để chọn giữa 3 chế độ xem bảng xếp hạng.
    """
    def __init__(self):
        options = [
            discord.SelectOption(
                label="🕒 Cỗ Máy Thời Gian (Top Giờ Focus)", 
                value="hours", 
                emoji="🕒", 
                description="Xếp hạng theo tổng số giờ tích lũy cày cuốc."
            ),
            discord.SelectOption(
                label="🔥 Kỷ Luật Sắt (Top Streak)", 
                value="streak", 
                emoji="🔥", 
                description="Xếp hạng theo chuỗi ngày hoàn thành liên tiếp."
            ),
            discord.SelectOption(
                label="🚀 Ngôi Sao Tuần (Top Tuần)", 
                value="week", 
                emoji="🚀", 
                description="Xếp hạng theo giờ focus trong tuần này (mới nhất)."
            )
        ]
        super().__init__(
            placeholder="Chọn chế độ xếp hạng...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="leaderboard_select"
        )

    async def callback(self, interaction: discord.Interaction):
        # Defer trước để tránh hết hạn tương tác
        await interaction.response.defer()
        
        category = self.values[0]
        page = 1
        
        cog = interaction.client.get_cog("Leaderboard")
        if not cog:
            return
            
        overall_stats = await cog.get_overall_stats()
        db_users = await cog.get_leaderboard_data(category)
        
        n_display = len(db_users[:100])
        n_codeblock = max(0, n_display - 3)
        if n_codeblock == 0:
            max_page = 1
        else:
            max_page = 1 + math.ceil(max(0, n_codeblock - 7) / 10)
            
        self.view.update_buttons(page, max_page)
        
        embed = await cog.build_leaderboard_embed(
            interaction.client, 
            interaction.guild, 
            category, 
            page, 
            db_users, 
            overall_stats
        )
        
        await interaction.edit_original_response(embed=embed, view=self.view)


class OtherProfileSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Chọn thành viên để xem hồ sơ...",
            min_values=1,
            max_values=1,
            custom_id="leaderboard_select_other_user"
        )

    async def callback(self, interaction: discord.Interaction):
        # Defer immediately to avoid timeout
        await interaction.response.defer(ephemeral=True)
        member = self.values[0]
        
        if member.bot:
            await interaction.followup.send("❌ Không thể xem hồ sơ năng suất của Bot!", ephemeral=True)
            return

        try:
            embed = await get_profile_embed(member)
            await interaction.followup.send(
                content=f"📊 Đây là hồ sơ năng suất của **{member.display_name}**:",
                embed=embed,
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Lỗi khi tải hồ sơ của {member.display_name}: {e}")
            await interaction.followup.send(f"❌ Có lỗi xảy ra khi tải hồ sơ: {e}", ephemeral=True)


class OtherProfileSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(OtherProfileSelect())


class LeaderboardView(discord.ui.View):
    """
    Giao diện View tương tác chứa Dropdown chọn tab, nút lật trang và nút 'Hạng của tôi?'.
    Dùng custom_id cố định và timeout=None để hoạt động Persistent qua các lần bot restart.
    """
    def __init__(self):
        super().__init__(timeout=None)
        # Thêm dropdown vào View
        self.add_item(LeaderboardDropdown())

    @discord.ui.button(
        label="⏪ Trang trước",
        style=discord.ButtonStyle.secondary,
        custom_id="leaderboard_btn_prev"
    )
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        cog = interaction.client.get_cog("Leaderboard")
        if not cog:
            return
            
        category, page = cog.parse_leaderboard_state(interaction.message)
        page = max(1, page - 1)
        
        overall_stats = await cog.get_overall_stats()
        db_users = await cog.get_leaderboard_data(category)
        
        n_display = len(db_users[:100])
        n_codeblock = max(0, n_display - 3)
        if n_codeblock == 0:
            max_page = 1
        else:
            max_page = 1 + math.ceil(max(0, n_codeblock - 7) / 10)
            
        page = min(page, max_page)
        self.update_buttons(page, max_page)
        
        embed = await cog.build_leaderboard_embed(
            interaction.client, 
            interaction.guild, 
            category, 
            page, 
            db_users, 
            overall_stats
        )
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(
        label="Trang 1/1",
        style=discord.ButtonStyle.secondary,
        custom_id="leaderboard_btn_info",
        disabled=True
    )
    async def page_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass # Nút tĩnh chỉ hiển thị trang số

    @discord.ui.button(
        label="Trang tiếp ⏩",
        style=discord.ButtonStyle.secondary,
        custom_id="leaderboard_btn_next"
    )
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        cog = interaction.client.get_cog("Leaderboard")
        if not cog:
            return
            
        category, page = cog.parse_leaderboard_state(interaction.message)
        page += 1
        
        overall_stats = await cog.get_overall_stats()
        db_users = await cog.get_leaderboard_data(category)
        
        n_display = len(db_users[:100])
        n_codeblock = max(0, n_display - 3)
        if n_codeblock == 0:
            max_page = 1
        else:
            max_page = 1 + math.ceil(max(0, n_codeblock - 7) / 10)
            
        page = min(page, max_page)
        self.update_buttons(page, max_page)
        
        embed = await cog.build_leaderboard_embed(
            interaction.client, 
            interaction.guild, 
            category, 
            page, 
            db_users, 
            overall_stats
        )
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(
        label="🎯 Hạng của tôi?",
        style=discord.ButtonStyle.success,
        custom_id="leaderboard_btn_find_rank"
    )
    async def find_my_rank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        cog = interaction.client.get_cog("Leaderboard")
        if not cog:
            return
            
        category, _ = cog.parse_leaderboard_state(interaction.message)
        db_users = await cog.get_leaderboard_data(category)
        
        user_id = interaction.user.id
        user_idx = -1
        for idx, row in enumerate(db_users):
            if row[0] == user_id:
                user_idx = idx
                break
                
        if user_idx == -1:
            await interaction.followup.send(
                "❌ **Bạn chưa có tên trên bảng xếp hạng này!**\n"
                "Hãy đăng ký mục tiêu bằng nút trên Trung Tâm Điều Khiển và hoàn thành ít nhất một phiên Focus thành công để được ghi nhận nhé.",
                ephemeral=True
            )
            return
            
        rank = user_idx + 1
        user_row = db_users[user_idx]
        _, lvl, streak, val_mins = user_row
        
        # Định nghĩa nhãn & đơn vị đo lường tương ứng chế độ
        category_name = {
            "hours": "Tổng Giờ Focus",
            "streak": "Chuỗi Kỷ Luật",
            "week": "Giờ Focus Tuần"
        }.get(category, "")
        
        if category == "streak":
            user_val = streak
            unit = "ngày"
            user_val_str = f"{int(user_val)}"
        else:
            user_val = val_mins / 60.0
            unit = "giờ"
            user_val_str = f"{user_val:.1f}"
            
        try:
            profile_embed = await get_profile_embed(interaction.user)
        except Exception as e:
            logger.error(f"Lỗi khi tải hồ sơ của {interaction.user.display_name}: {e}")
            profile_embed = None

        # Trường hợp đứng hạng nhất
        if rank == 1:
            await interaction.followup.send(
                content=(
                    f"🏆 **Tuyệt vời!** Bạn đang đứng **Đầu Bảng Xếp Hạng (#{rank})** ở hạng mục *{category_name}* với **`{user_val_str}` {unit}** (Cấp {lvl}).\n"
                    f"Hãy duy trì phong độ xuất sắc này để bảo vệ ngôi vương nhé! 💪"
                ),
                embed=profile_embed,
                ephemeral=True
            )
            return
            
        # Lấy người trực tiếp xếp ngay trên mình
        above_row = db_users[user_idx - 1]
        above_id, above_lvl, above_streak, above_val_mins = above_row
        
        if category == "streak":
            above_val = above_streak
            above_val_str = f"{int(above_val)}"
            diff = above_val - user_val
            needed_text = f"duy trì thêm **{int(max(1, diff))} ngày** streak nữa"
        else:
            above_val = above_val_mins / 60.0
            above_val_str = f"{above_val:.1f}"
            diff = above_val - user_val
            needed_text = f"focus thêm **{max(0.1, diff):.1f} giờ** nữa"
            
        above_name = await cog.get_user_display_name(interaction.client, interaction.guild, above_id)
        
        await interaction.followup.send(
            content=(
                f"🎯 Bạn đang ở hạng **#{rank}** với **`{user_val_str}` {unit}** ở hạng mục *{category_name}*.\n\n"
                f"Cố lên! Bạn chỉ cần {needed_text} để đánh bại **{above_name}** (đang xếp hạng **#{rank - 1}** với `{above_val_str}` {unit})!"
            ),
            embed=profile_embed,
            ephemeral=True
        )

    @discord.ui.button(
        label="🔍 Xem hồ sơ người khác",
        style=discord.ButtonStyle.primary,
        custom_id="leaderboard_btn_view_other"
    )
    async def view_other_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = OtherProfileSelectView()
        await interaction.response.send_message(
            content="👥 **Vui lòng chọn thành viên bên dưới để xem hồ sơ năng suất của họ:**",
            view=view,
            ephemeral=True
        )

    def update_buttons(self, page: int, max_page: int):
        """Cập nhật trạng thái bật/tắt và nhãn cho các nút lật trang."""
        self.prev_page.disabled = (page <= 1)
        self.page_info.label = f"Trang {page}/{max_page}"
        self.next_page.disabled = (page >= max_page)


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Đăng ký view tương tác để bot lắng nghe callback vĩnh viễn
        self.bot.add_view(LeaderboardView())
        
        # Bắt đầu vòng lặp tự động cập nhật BXH mỗi 15 phút
        self.update_leaderboard_loop.start()
        self.scheduler = None

    def cog_unload(self):
        self.update_leaderboard_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("Leaderboard Cog đã sẵn sàng.")
        
        # Tìm hoặc tạo bộ lập lịch APScheduler tương thích
        tracker_cog = self.bot.get_cog("Tracker")
        if tracker_cog and hasattr(tracker_cog, "scheduler") and tracker_cog.scheduler.running:
            self.scheduler = tracker_cog.scheduler
            logger.info("Đã chia sẻ dùng chung AsyncIOScheduler của Tracker Cog.")
        else:
            self.scheduler = AsyncIOScheduler()
            self.scheduler.start()
            logger.info("Đã khởi tạo AsyncIOScheduler độc lập cho Leaderboard Cog.")

        # Đăng ký Job kiểm tra phát thưởng lúc 23:59 Chủ Nhật hàng tuần
        self.scheduler.add_job(
            self.check_and_distribute_weekly_rewards,
            "cron",
            day_of_week="sun",
            hour=23,
            minute=59,
            id="leaderboard_weekly_rewards",
            replace_existing=True
        )
        logger.info("Đã thiết lập Job quét phần thưởng BXH Tuần (23:59 Chủ Nhật).")

        # Quét kiểm tra ngay lập tức khi startup để bù đắp phần thưởng nếu bot bị offline đúng lúc
        await self.check_and_distribute_weekly_rewards()

    async def get_user_display_name(self, bot, guild, user_id: int) -> str:
        """Helper tìm nạp nhanh Tên hiển thị của thành viên trên Discord."""
        member = guild.get_member(user_id)
        if member:
            return member.display_name
            
        user = bot.get_user(user_id)
        if user:
            return user.display_name
            
        try:
            user = await bot.fetch_user(user_id)
            return user.display_name
        except Exception:
            return f"User_{user_id}"

    async def get_overall_stats(self) -> dict:
        """Truy vấn tổng giờ cày và xếp hạng tương ứng trên toàn server để tính Tier chuẩn xác."""
        async with get_db_session() as session:
            stmt = (
                select(
                    User.user_id,
                    func.coalesce(func.sum(FocusSession.actual_duration), 0).label("total_mins")
                )
                .outerjoin(FocusSession, (User.user_id == FocusSession.user_id) & (FocusSession.status == "completed"))
                .group_by(User.user_id)
                .order_by(
                    func.coalesce(func.sum(FocusSession.actual_duration), 0).desc(),
                    User.level.desc(),
                    User.user_id.asc()
                )
            )
            res = await session.execute(stmt)
            rows = res.all()
            
            overall_stats = {}
            for rank, (uid, mins) in enumerate(rows, start=1):
                overall_stats[uid] = (mins / 60.0, rank)
            return overall_stats

    async def get_leaderboard_data(self, category: str, start_of_week: datetime.datetime = None):
        """Truy vấn danh sách User từ Database và sắp xếp theo điều kiện của từng tab."""
        if start_of_week is None:
            now = datetime.datetime.now()
            # Tính mốc bắt đầu tuần này (Thứ 2) lúc 00:00:00
            start_of_week = now - datetime.timedelta(days=now.weekday())
            start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
            
        end_of_week = start_of_week + datetime.timedelta(days=7)

        async with get_db_session() as session:
            if category == "hours":
                stmt = (
                    select(
                        User.user_id,
                        User.level,
                        User.current_streak,
                        func.coalesce(func.sum(FocusSession.actual_duration), 0).label("val")
                    )
                    .outerjoin(FocusSession, (User.user_id == FocusSession.user_id) & (FocusSession.status == "completed"))
                    .group_by(User.user_id)
                    .order_by(
                        func.coalesce(func.sum(FocusSession.actual_duration), 0).desc(),
                        User.level.desc(),
                        User.user_id.asc()
                    )
                )
            elif category == "streak":
                stmt = (
                    select(
                        User.user_id,
                        User.level,
                        User.current_streak,
                        func.coalesce(func.sum(FocusSession.actual_duration), 0).label("val")
                    )
                    .outerjoin(FocusSession, (User.user_id == FocusSession.user_id) & (FocusSession.status == "completed"))
                    .group_by(User.user_id)
                    .order_by(
                        User.current_streak.desc(),
                        func.coalesce(func.sum(FocusSession.actual_duration), 0).desc(),
                        User.level.desc(),
                        User.user_id.asc()
                    )
                )
            elif category == "week":
                stmt = (
                    select(
                        User.user_id,
                        User.level,
                        User.current_streak,
                        func.coalesce(func.sum(FocusSession.actual_duration), 0).label("val")
                    )
                    .outerjoin(
                        FocusSession,
                        (User.user_id == FocusSession.user_id) & 
                        (FocusSession.status == "completed") & 
                        (FocusSession.start_time >= start_of_week) &
                        (FocusSession.start_time < end_of_week)
                    )
                    .group_by(User.user_id)
                    .order_by(
                        func.coalesce(func.sum(FocusSession.actual_duration), 0).desc(),
                        User.level.desc(),
                        User.user_id.asc()
                    )
                )
            else:
                raise ValueError(f"Tab {category} không hợp lệ.")
                
            res = await session.execute(stmt)
            return res.all()

    def parse_leaderboard_state(self, message: discord.Message) -> tuple[str, int]:
        """Phân tích footer của tin nhắn để phục hồi trạng thái Trang và Tab hiện tại."""
        try:
            footer = message.embeds[0].footer.text
            parts = footer.split(" | ")
            page_part = parts[0].replace("Trang ", "")
            page = int(page_part.split("/")[0])
            category = parts[1].replace("Chế độ: ", "")
            return category, page
        except Exception:
            return "hours", 1

    async def build_leaderboard_embed(self, bot, guild, category: str, page: int, db_users, overall_stats) -> discord.Embed:
        """Dựng cấu trúc Embed hiển thị Bảng xếp hạng hoàn chỉnh."""
        if category == "hours":
            title = "🏆 ĐẠI LỘ DANH VỌNG: TOP GIỜ FOCUS 🕒"
            desc = "*Nơi tôn vinh những cỗ máy thời gian bền bỉ, cày cuốc ngày đêm.*"
            color = discord.Color.purple()
        elif category == "streak":
            title = "🏆 ĐẠI LỘ DANH VỌNG: TOP CHUỖI KỶ LUẬT 🔥"
            desc = "*Nơi tôn vinh tinh thần kỷ luật sắt, kiên trì hoàn thành mục tiêu mỗi ngày.*"
            color = discord.Color.red()
        elif category == "week":
            title = "🏆 ĐẠI LỘ DANH VỌNG: TOP NGÔI SAO TUẦN 🚀"
            desc = "*Đua top tuần cực kỳ nảy lửa! Bảng xếp hạng sẽ tự động reset vào 23:59 Chủ Nhật.*"
            color = discord.Color.gold()
        else:
            raise ValueError(f"Tab {category} không hợp lệ.")

        # Lọc chỉ hiển thị những thành viên thực tế đang ở trong Guild
        filtered_users = []
        for row in db_users:
            uid = row[0]
            if guild.get_member(uid):
                filtered_users.append(row)
                
        total_users = len(filtered_users)
        display_users = filtered_users[:100]
        n_display = len(display_users)
        
        n_codeblock = max(0, n_display - 3)
        if n_codeblock == 0:
            max_page = 1
        else:
            max_page = 1 + math.ceil(max(0, n_codeblock - 7) / 10)
            
        page = max(1, min(page, max_page))
        
        embed = discord.Embed(
            title=title,
            description=desc,
            color=color
        )
        embed.set_thumbnail(url=bot.user.display_avatar.url if bot.user else None)

        # 👑 TOP 3: Tôn vinh Tuyệt đối (The Podium)
        podium_emojis = ["🥇", "🥈", "🥉"]
        for i in range(3):
            emoji = podium_emojis[i]
            rank = i + 1
            
            if i < n_display:
                uid, lvl, streak, val_mins = display_users[i]
                display_name = await self.get_user_display_name(bot, guild, uid)
                
                # Tính Bậc/Danh hiệu dựa trên tổng giờ cày
                tot_h, o_rank = overall_stats.get(uid, (0.0, 9999))
                tier, title_name = get_tier_and_title(tot_h, o_rank)
                
                # Trình bày nội dung phù hợp cho từng tab
                if category == "hours":
                    hours = val_mins / 60.0
                    value_str = f"Cấp: {lvl} | ⏳ {hours:.1f} giờ | 🔥 Streak: {streak} ngày"
                elif category == "streak":
                    tot_h_val = val_mins / 60.0
                    value_str = f"Cấp: {lvl} | 🔥 Streak: {streak} ngày | ⏳ {tot_h_val:.1f} giờ"
                elif category == "week":
                    wk_hours = val_mins / 60.0
                    value_str = f"Cấp: {lvl} | ⏳ Tuần này: {wk_hours:.1f} giờ | 🔥 Streak: {streak} ngày"
                
                field_title = f"{emoji} Hạng {rank}: {display_name} — {title_name}"
                field_value = f"Bậc: **{tier}** | {value_str}"
            else:
                field_title = f"{emoji} Hạng {rank}: Trống"
                field_value = "Bậc: **Đồng** | Cấp: 0 | ⏳ 0 giờ | 🔥 Streak: 0 ngày"
                
            embed.add_field(name=field_title, value=field_value, inline=False)

        # 🏅 TOP 4 đến 100: Giao diện Bảng (The Grid)
        if page == 1:
            start_idx = 3
            end_idx = 10
        else:
            start_idx = 10 + (page - 2) * 10
            end_idx = start_idx + 10
            
        page_users = display_users[start_idx:end_idx]
        
        table_content = ""
        if page_users:
            table_rows = []
            for idx, row in enumerate(page_users, start=start_idx + 1):
                uid, lvl, streak, val_mins = row
                display_name = await self.get_user_display_name(bot, guild, uid)
                
                hours = val_mins / 60.0
                hours_text = f"{hours:.1f}h"
                
                rank_cell = format_cell(idx, 3)
                name_cell = format_cell(display_name, 14)
                level_cell = format_cell(lvl, 4)
                hours_cell = format_cell(hours_text, 6)
                streak_cell = format_cell(streak, 6)
                
                table_rows.append(f"{rank_cell}| {name_cell}| {level_cell}| {hours_cell}| {streak_cell}")
                
            header = "#  | Tên Người Dùng | Cấp | Giờ   | Chuỗi"
            sep    = "---|----------------|-----|-------|-----"
            table_content = f"```\n{header}\n{sep}\n" + "\n".join(table_rows) + "\n```"
        else:
            table_content = "*(Bảng trống)*"
            
        embed.add_field(name="🏅 CÁC THÀNH VIÊN TIẾP THEO", value=table_content, inline=False)
        embed.set_footer(text=f"Trang {page}/{max_page} | Chế độ: {category} | Chronos Bot")
        return embed

    async def refresh_leaderboard_messages(self, force_channel=None):
        """Quét tìm kênh, dọn dẹp và gửi cập nhật nội dung bảng xếp hạng."""
        for guild in self.bot.guilds:
            if force_channel:
                channel = force_channel
            else:
                channel = discord.utils.get(guild.text_channels, name="🏆-dai-lo-danh-vong")
                if not channel:
                    channel = discord.utils.get(guild.text_channels, name="dai-lo-danh-vong")
                if not channel:
                    channel = guild.get_channel(config.KENH_BXH_ID)
                    
            if not channel:
                logger.warning(f"Không tìm thấy kênh Bảng xếp hạng tại server '{guild.name}'.")
                continue
                
            permissions = channel.permissions_for(guild.me)
            if not (permissions.read_messages and permissions.send_messages and permissions.manage_messages and permissions.read_message_history):
                logger.error(f"Bot thiếu quyền hạn tương tác tại kênh #{channel.name} server '{guild.name}'.")
                continue
                
            try:
                category = "hours"
                page = 1
                
                # Đọc lịch sử gần nhất để tái sử dụng tin nhắn cũ
                messages = []
                async for message in channel.history(limit=5):
                    messages.append(message)
                    
                should_recreate = True
                target_message = None
                
                if len(messages) == 1:
                    msg = messages[0]
                    if msg.author.id == self.bot.user.id and msg.embeds:
                        if "ĐẠI LỘ DANH VỌNG" in msg.embeds[0].title:
                            should_recreate = False
                            target_message = msg
                            # Phục hồi tab & trang người dùng đang xem để tránh gián đoạn
                            category, page = self.parse_leaderboard_state(msg)
                            
                overall_stats = await self.get_overall_stats()
                db_users = await self.get_leaderboard_data(category)
                
                n_display = len(db_users[:100])
                n_codeblock = max(0, n_display - 3)
                if n_codeblock == 0:
                    max_page = 1
                else:
                    max_page = 1 + math.ceil(max(0, n_codeblock - 7) / 10)
                    
                page = min(page, max_page)
                
                embed = await self.build_leaderboard_embed(
                    self.bot, 
                    guild, 
                    category, 
                    page, 
                    db_users, 
                    overall_stats
                )
                
                view = LeaderboardView()
                view.update_buttons(page, max_page)
                
                if should_recreate:
                    logger.info(f"Đang dọn dẹp và khởi tạo mới Bảng xếp hạng tại #{channel.name} server '{guild.name}'...")
                    await channel.purge(limit=100)
                    await channel.send(embed=embed, view=view)
                else:
                    logger.info(f"Đang cập nhật lại dữ liệu Bảng xếp hạng tại #{channel.name} server '{guild.name}'...")
                    await target_message.edit(embed=embed, view=view)
                    
            except Exception as e:
                logger.error(f"Lỗi khi xử lý làm mới bảng xếp hạng tại server '{guild.name}': {e}")

    @tasks.loop(minutes=15)
    async def update_leaderboard_loop(self):
        """Loop định kỳ cập nhật thông tin BXH mỗi 15 phút."""
        logger.info("Đang chạy tác vụ tự động quét cập nhật Bảng xếp hạng định kỳ...")
        await self.refresh_leaderboard_messages()

    @update_leaderboard_loop.before_loop
    async def before_update_leaderboard(self):
        await self.bot.wait_until_ready()

    @commands.hybrid_command(
        name="leaderboard_refresh", 
        description="[Admin Only] Khởi tạo lại hoặc dọn dẹp và cập nhật bảng xếp hạng"
    )
    @commands.has_permissions(administrator=True)
    async def leaderboard_refresh(self, ctx: commands.Context):
        """Khởi tạo hoặc làm mới bảng xếp hạng thủ công trực tiếp tại kênh hiện tại."""
        await ctx.defer(ephemeral=True)
        try:
            await self.refresh_leaderboard_messages(force_channel=ctx.channel)
            await ctx.send("✅ Đã dọn dẹp kênh và cập nhật lại Bảng xếp hạng thành công!", ephemeral=True)
        except Exception as e:
            logger.error(f"Lỗi khi chạy lệnh làm mới BXH thủ công: {e}")
            await ctx.send(f"❌ Lỗi hệ thống: {e}", ephemeral=True)

    async def check_and_distribute_weekly_rewards(self):
        """Kiểm tra điều kiện thời gian và tiến hành trao giải thưởng tuần."""
        now = datetime.datetime.now()
        weekday = now.weekday() # Monday=0, ..., Sunday=6
        
        # Xác định mốc ngày Chủ Nhật của tuần cần tổng kết
        if weekday == 6: # Hôm nay là Chủ Nhật
            if now.time() >= datetime.time(23, 59, 0):
                target_sunday = now.date()
            else:
                target_sunday = now.date() - datetime.timedelta(days=7)
        else:
            target_sunday = now.date() - datetime.timedelta(days=(weekday + 1))
            
        week_key = target_sunday.strftime("%Y-%m-%d")
        
        # Kiểm tra xem tuần này đã được phát quà trước đó chưa
        last_rewarded = get_last_rewarded_week()
        if last_rewarded == week_key:
            return
            
        logger.info(f"Phát hiện tuần chưa trao giải: {week_key}. Bắt đầu tổng kết...")
        
        # Xác định mốc Thứ 2 bắt đầu tuần của ngày target_sunday
        target_monday = target_sunday - datetime.timedelta(days=6)
        target_week_start = datetime.datetime.combine(target_monday, datetime.time.min)
        
        # Truy vấn dữ liệu Top tuần của đúng tuần đó
        db_users = await self.get_leaderboard_data("week", start_of_week=target_week_start)
        
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild:
            logger.warning("Bot chưa tham gia server nào, trì hoãn phát thưởng.")
            return

        # Lọc chỉ lấy thành viên thực tế trong server có số phút làm việc > 0
        active_winners = []
        for row in db_users:
            uid = row[0]
            if guild.get_member(uid) and row[3] > 0:
                active_winners.append(row)
        winners = active_winners[:3]
        
        if not winners:
            logger.info("Không có thành viên nào tập trung trong tuần vừa qua. Bỏ qua phát quà.")
            set_last_rewarded_week(week_key)
            return
            
        # Tìm danh hiệu role 'Vua Kỷ Luật Tuần'
        role = discord.utils.get(guild.roles, name="Vua Kỷ Luật Tuần")
        if not role:
            try:
                role = await guild.create_role(
                    name="Vua Kỷ Luật Tuần",
                    color=discord.Color.gold(),
                    hoist=True,
                    reason="Role vinh danh tuần"
                )
                logger.info("Đã khởi tạo Role 'Vua Kỷ Luật Tuần' thành công.")
            except Exception as e:
                logger.error(f"Lỗi khi khởi tạo Role vinh danh tuần: {e}")
                
        # 1. Gỡ danh hiệu của toàn bộ người dùng cũ
        if role:
            for member in role.members:
                try:
                    await member.remove_roles(role, reason="Thu hồi danh hiệu tuần cũ")
                except Exception as e:
                    logger.error(f"Không thể gỡ role từ {member.display_name}: {e}")
                    
        # 2. Cộng tiền và gán role cho Top 3 mới
        rewards = [150, 100, 50]
        winner_members = []
        
        async with get_db_session() as session:
            for idx, row in enumerate(winners):
                uid, lvl, streak, val_mins = row
                tokens_reward = rewards[idx]
                
                # Cộng Token
                user_res = await session.execute(select(User).filter_by(user_id=uid))
                user = user_res.scalar_one_or_none()
                if user:
                    user.token_balance += tokens_reward
            await session.commit()
            
        # Tách biệt Discord API calls ra ngoài database session block
        for idx, row in enumerate(winners):
            uid, lvl, streak, val_mins = row
            tokens_reward = rewards[idx]
            member = guild.get_member(uid)
            if member:
                winner_members.append((member, val_mins / 60.0, tokens_reward))
                if role:
                    try:
                        await member.add_roles(role, reason=f"Top {idx+1} Focus tuần {week_key}")
                    except Exception as e:
                        logger.error(f"Lỗi khi gán role cho {member.display_name}: {e}")
            else:
                disp_name = await self.get_user_display_name(self.bot, guild, uid)
                winner_members.append((disp_name, val_mins / 60.0, tokens_reward))
            
        set_last_rewarded_week(week_key)
        
        # 3. Gửi thông báo vinh danh lên kênh thông báo
        ann_channel = guild.get_channel(config.KENH_THONG_BAO_ID)
        if not ann_channel:
            ann_channel = discord.utils.get(guild.text_channels, name="thong-bao")
        if not ann_channel:
            ann_channel = discord.utils.get(guild.text_channels, name="🏆-dai-lo-danh-vong")
            
        if ann_channel:
            podium_lines = []
            medals = ["🥇", "🥈", "🥉"]
            for idx, (m, hrs, tk) in enumerate(winner_members):
                m_str = m.mention if isinstance(m, discord.Member) else str(m)
                podium_lines.append(f"{medals[idx]} **Top {idx+1}:** {m_str} — `{hrs:.1f} giờ` (+{tk} Tokens 🪙)")
                
            podium_text = "\n".join(podium_lines)
            
            embed = discord.Embed(
                title="🏆 VINH DANH: VUA KỶ LUẬT TUẦN 🏆",
                description=(
                    f"Đã đến hạn tổng kết Bảng Xếp Hạng Tuần! ({week_key})\n\n"
                    f"Hệ thống tự hào vinh danh các cá nhân xuất sắc nhất tuần qua đã dành danh hiệu **Vua Kỷ Luật Tuần**:\n\n"
                    f"{podium_text}\n\n"
                    f"👑 Các chiến binh trên đã được gán **Role Vua Kỷ Luật Tuần** đặc biệt lấp lánh (thời hạn 7 ngày). "
                    f"Cảm ơn sự nỗ lực tập trung vượt trội của các bạn! 🚀"
                ),
                color=discord.Color.gold(),
                timestamp=now
            )
            try:
                await ann_channel.send(embed=embed)
            except Exception as e:
                logger.error(f"Lỗi gửi tin vinh danh: {e}")

    @commands.hybrid_command(
        name="leaderboard_trigger_weekly", 
        description="[Admin Only] Ép buộc chạy tiến trình tổng kết và trao thưởng tuần ngay lập tức"
    )
    @commands.has_permissions(administrator=True)
    async def leaderboard_trigger_weekly(self, ctx: commands.Context):
        """Kích hoạt khẩn cấp phát quà tuần (Test thủ công) mà không cần chờ Chủ Nhật."""
        await ctx.defer(ephemeral=True)
        try:
            logger.info(f"Admin {ctx.author.name} kích hoạt khẩn cấp trao giải tuần.")
            
            now = datetime.datetime.now()
            week_key = "FORCED-" + now.strftime("%Y-%m-%d-%H-%M-%S")
            
            db_users = await self.get_leaderboard_data("week")
            # Lọc chỉ lấy thành viên thực tế trong server có số phút làm việc > 0
            active_winners = []
            for row in db_users:
                uid = row[0]
                if ctx.guild.get_member(uid) and row[3] > 0:
                    active_winners.append(row)
            winners = active_winners[:3]
            
            if not winners:
                await ctx.send("❌ Không tìm thấy ai thực sự trong server có giờ tích lũy tuần này (> 0h) để phát quà.", ephemeral=True)
                return
                
            role = discord.utils.get(ctx.guild.roles, name="Vua Kỷ Luật Tuần")
            if not role:
                role = await ctx.guild.create_role(
                    name="Vua Kỷ Luật Tuần",
                    color=discord.Color.gold(),
                    hoist=True,
                    reason="Tạo Role vinh danh"
                )
                
            for member in role.members:
                try:
                    await member.remove_roles(role, reason="Reset danh hiệu cưỡng bức")
                except Exception:
                    pass
                    
            rewards = [150, 100, 50]
            winner_members = []
            
            async with get_db_session() as session:
                for idx, row in enumerate(winners):
                    uid, lvl, streak, val_mins = row
                    tokens_reward = rewards[idx]
                    
                    user_res = await session.execute(select(User).filter_by(user_id=uid))
                    user = user_res.scalar_one_or_none()
                    if user:
                        user.token_balance += tokens_reward
                await session.commit()
                
            # Tách biệt Discord API calls ra ngoài database session block
            for idx, row in enumerate(winners):
                uid, lvl, streak, val_mins = row
                tokens_reward = rewards[idx]
                member = ctx.guild.get_member(uid)
                if member:
                    winner_members.append((member, val_mins / 60.0, tokens_reward))
                    try:
                        await member.add_roles(role, reason=f"Top {idx+1} Focus Tuần (Forced)")
                    except Exception as e:
                        logger.error(f"Không thể gán role cho {member.display_name}: {e}")
                else:
                    disp_name = await self.get_user_display_name(self.bot, ctx.guild, uid)
                    winner_members.append((disp_name, val_mins / 60.0, tokens_reward))
                
            ann_channel = ctx.guild.get_channel(config.KENH_THONG_BAO_ID)
            if not ann_channel:
                ann_channel = discord.utils.get(ctx.guild.text_channels, name="thong-bao")
            if not ann_channel:
                ann_channel = ctx.channel
                
            if ann_channel:
                podium_lines = []
                medals = ["🥇", "🥈", "🥉"]
                for idx, (m, hrs, tk) in enumerate(winner_members):
                    m_str = m.mention if isinstance(m, discord.Member) else str(m)
                    podium_lines.append(f"{medals[idx]} **Top {idx+1}:** {m_str} — `{hrs:.1f} giờ` (+{tk} Tokens 🪙)")
                    
                podium_text = "\n".join(podium_lines)
                
                embed = discord.Embed(
                    title="🏆 THỬ NGHIỆM VINH DANH: VUA KỶ LUẬT TUẦN 🏆",
                    description=(
                        f"Đây là lượt trao thưởng thử nghiệm được kích hoạt bởi Admin.\n\n"
                        f"Hệ thống vinh danh các cá nhân xuất sắc nhất tuần này:\n\n"
                        f"{podium_text}\n\n"
                        f"👑 Các chiến binh trên đã được gán **Role Vua Kỷ Luật Tuần** đặc biệt lấp lánh (thời hạn 7 ngày)."
                    ),
                    color=discord.Color.gold(),
                    timestamp=now
                )
                await ann_channel.send(embed=embed)
                
            await ctx.send("✅ Đã chạy phát thưởng tuần thử nghiệm cưỡng bức thành công!", ephemeral=True)
            
        except Exception as e:
            logger.error(f"Lỗi khi kích hoạt phát thưởng tuần cưỡng bức: {e}")
            await ctx.send(f"❌ Lỗi hệ thống: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Leaderboard(bot))
