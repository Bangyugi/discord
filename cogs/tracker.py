import logging
import datetime
import math
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update
from database.db_session import get_db_session
from database.models import User, Task, FocusSession, Inventory
import config

logger = logging.getLogger("ChronosBot.Tracker")

class SessionWarningView(discord.ui.View):
    def __init__(self, tracker, group_user_ids: list, voice_channel_id: int):
        super().__init__(timeout=300)
        self.tracker = tracker
        self.group_user_ids = group_user_ids
        self.voice_channel_id = voice_channel_id
        self.message = None
        self.resolved = False

    @discord.ui.button(
        label="Tôi đã hiểu ✅",
        style=discord.ButtonStyle.green,
        custom_id="session_warning_understand"
    )
    async def understand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.group_user_ids:
            await interaction.response.send_message("❌ Bạn không thuộc phiên làm việc chuẩn bị kết thúc này.", ephemeral=True)
            return
        await interaction.response.send_message("✅ Bạn đã chọn 'Tôi đã hiểu'. Bạn sẽ tự động bị mời ra khỏi phòng voice khi hết giờ làm việc.", ephemeral=True)

    @discord.ui.button(
        label="☕ Cà phê đen đá",
        style=discord.ButtonStyle.blurple,
        custom_id="session_warning_use_coffee"
    )
    async def use_coffee(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message("❌ Phiên làm việc này đã được gia hạn bởi thành viên khác.", ephemeral=True)
            return

        user_id = interaction.user.id
        if user_id not in self.group_user_ids:
            await interaction.response.send_message("❌ Bạn không thuộc phiên làm việc này.", ephemeral=True)
            return

        # Check inventory for coffee
        async with get_db_session() as session:
            inv_res = await session.execute(
                select(Inventory).filter_by(user_id=user_id, item_id="coffee")
            )
            inv = inv_res.scalar_one_or_none()

            if not inv or inv.quantity <= 0:
                await interaction.response.send_message("❌ Bạn không có vật phẩm **☕ Cà Phê Đen Đá** trong túi đồ.", ephemeral=True)
                return

            # Deduct 1 coffee
            if inv.quantity > 1:
                inv.quantity -= 1
            else:
                await session.delete(inv)
            await session.commit()

        # Mark as resolved
        self.resolved = True

        # Extend sessions by 30 minutes for all users in the group who are still active
        extended_mentions = []
        for u_id in self.group_user_ids:
            if u_id in self.tracker.active_sessions:
                s_info = self.tracker.active_sessions[u_id]
                s_info["duration"] += 30
                s_info["warning_sent"] = False
                s_info["is_extended"] = True
                
                # Fetch member to mention
                member = interaction.guild.get_member(u_id)
                if member:
                    extended_mentions.append(member.mention)

        # Disable all buttons
        for child in self.children:
            child.disabled = True

        # Update warning message
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.description = (
            f"☕ **{interaction.user.mention} đã sử dụng 1 Cà Phê Đen Đá để gia hạn thêm 30 phút cho cả phòng!**\n\n"
            f"• **Các thành viên được gia hạn:** {', '.join(extended_mentions)}\n"
            f"• **Thời gian nới rộng:** Vẫn sẽ tính token nhận thêm, nhưng không phạt nếu rời phòng voice trong thời gian gia hạn này."
        )
        await interaction.message.edit(embed=embed, view=None)
        await interaction.response.send_message("🎉 Bạn đã gia hạn ca Focus thêm 30 phút thành công cho cả phòng!", ephemeral=True)

    async def on_timeout(self):
        if not self.resolved and self.message:
            try:
                for child in self.children:
                    child.disabled = True
                await self.message.edit(view=None)
            except Exception:
                pass


class Tracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler()
        
        # Bộ nhớ đệm theo dõi phiên làm việc trực tuyến (In-Memory tracking)
        # user_id -> {task_id, session_id, start_time, duration, task_title, voice_channel_id, disconnect_time}
        self.active_sessions = {}
        
        # Bộ nhớ đệm theo dõi thời gian 5 phút chờ điểm danh (Grace Period)
        # user_id -> {task_id, start_time, duration, task_title}
        self.grace_sessions = {}

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("Tracker Cog đã sẵn sàng.")
        
        # Khởi chạy Scheduler nếu chưa chạy
        if not self.scheduler.running:
            self.scheduler.start()
            # Đăng ký Job kiểm tra mỗi phút chạy tại giây thứ 0
            self.scheduler.add_job(self.scan_tasks_job, "cron", second=0)
            logger.info("APScheduler đã được bắt đầu và đăng ký tác vụ quét mỗi phút.")
            
        # Khôi phục các active session cũ từ DB (nếu bot bị restart bất thình lình)
        await self.recover_active_sessions()

    async def recover_active_sessions(self):
        """
        Khôi phục trạng thái giám sát các phiên Focus có trạng thái 'pending' trước khi bot restart.
        """
        logger.info("Đang kiểm tra và khôi phục các phiên Focus chưa hoàn thành trước đó...")
        now = datetime.datetime.now()
        
        pending_data = []
        
        async with get_db_session() as session:
            # Tìm các session có trạng thái 'pending'
            res = await session.execute(
                select(FocusSession).where(FocusSession.status == "pending")
            )
            pending_sessions = res.scalars().all()
            
            for fs in pending_sessions:
                # Tìm nạp thông tin Task tương ứng
                task_res = await session.execute(
                    select(Task).where(Task.task_id == fs.task_id)
                )
                task = task_res.scalar_one_or_none()
                if not task:
                    fs.status = "failed"
                    continue
                
                pending_data.append({
                    "session_id": fs.session_id,
                    "user_id": fs.user_id,
                    "start_time": fs.start_time,
                    "task_id": task.task_id,
                    "task_title": task.title,
                    "task_duration": task.duration_minutes,
                    "task_type": task.task_type
                })
            
            await session.commit()
            
        # Xử lý sau khi Session đã đóng
        for item in pending_data:
            user_id = item["user_id"]
            session_id = item["session_id"]
            start_time = item["start_time"]
            task_id = item["task_id"]
            task_title = item["task_title"]
            task_duration = item["task_duration"]
            task_type = item["task_type"]
            
            # Tìm xem người dùng đang ở kênh thoại nào
            user_vc_id = None
            for guild in self.bot.guilds:
                member = guild.get_member(user_id)
                if member and member.voice and member.voice.channel:
                    user_vc_id = member.voice.channel.id
                    break
            
            # Nếu người dùng vẫn ở trong phòng voice
            if user_vc_id:
                # Tính thời lượng còn lại
                elapsed_minutes = (now - start_time).total_seconds() / 60
                if elapsed_minutes >= task_duration:
                    # Nếu thời gian trôi qua đã đủ trong lúc bot offline, hoàn thành xuất sắc luôn!
                    await self.complete_focus_session(user_id, session_id, {"task_title": task_title, "duration": task_duration}, now)
                else:
                    # Nếu chưa đủ thời gian, tiếp tục theo dõi
                    self.active_sessions[user_id] = {
                        "task_id": task_id,
                        "session_id": session_id,
                        "start_time": start_time,
                        "duration": task_duration,
                        "task_title": task_title,
                        "voice_channel_id": user_vc_id,
                        "disconnect_time": None,
                        "task_type": task_type,
                        "original_duration": task_duration,
                        "is_extended": False,
                        "warning_sent": False
                    }
                    logger.info(f"Đã khôi phục theo dõi Focus cho User ID {user_id} (Task: {task_title})")
            else:
                # Nếu user đã thoát voice trong lúc bot offline, đánh dấu thất bại
                async with get_db_session() as session:
                    await session.execute(
                        update(FocusSession)
                        .where(FocusSession.session_id == session_id)
                        .values(status="failed")
                    )
                    await session.commit()
                    
                # Phạt người dùng vì không duy trì kỷ luật
                punishment_cog = self.bot.get_cog("Punishment")
                if punishment_cog:
                    guild_id = self.bot.guilds[0].id if self.bot.guilds else 0
                    for g in self.bot.guilds:
                        if g.get_member(user_id):
                            guild_id = g.id
                            break
                    await punishment_cog.apply_punishment(
                        user_id, 
                        guild_id, 
                        f"Không có mặt trong phòng voice khi hệ thống khởi động lại (đứt quãng phiên Focus '{task_title}')"
                    )

    async def scan_tasks_job(self):
        """
        Job chạy mỗi phút để thực hiện quét nhắc nhở, kiểm tra bắt đầu và xử lý hết hạn grace period.
        """
        logger.info("Đang thực hiện quét Task biểu hằng ngày...")
        now = datetime.datetime.now()
        current_minutes = now.hour * 60 + now.minute

        all_tasks_data = []
        async with get_db_session() as session:
            # 1. QUÉT KHỞI TẠO VÀ NHẮC NHỞ
            res = await session.execute(select(Task))
            all_tasks = res.scalars().all()
            for task in all_tasks:
                all_tasks_data.append({
                    "user_id": task.user_id,
                    "title": task.title,
                    "start_date": task.start_date,
                    "start_time": task.start_time,
                    "duration_minutes": task.duration_minutes,
                    "task_type": task.task_type,
                    "task_id": task.task_id
                })

        class DummyTask:
            def __init__(self, data):
                self.user_id = data["user_id"]
                self.title = data["title"]
                self.start_date = data["start_date"]
                self.start_time = data["start_time"]
                self.duration_minutes = data["duration_minutes"]
                self.task_type = data["task_type"]
                self.task_id = data["task_id"]

        for t_data in all_tasks_data:
            # Chỉ chạy đúng ngày bắt đầu của task (không lặp lại hằng ngày)
            if t_data["start_date"] and now.date() != t_data["start_date"]:
                continue

            task_minutes = t_data["start_time"].hour * 60 + t_data["start_time"].minute
            
            # A. Gửi nhắc nhở trước 5 phút (Hoặc wrap quanh ngày)
            if (task_minutes - current_minutes) % 1440 == 5:
                await self.send_dm_reminder(t_data["user_id"], t_data["title"])

            # B. Đúng giờ G bắt đầu Focus
            elif task_minutes == current_minutes:
                await self.handle_task_start_time(DummyTask(t_data))

        # 2. KIỂM TRA HẾT HẠN GRACE PERIOD (VẮNG MẶT QUÁ 5 PHÚT)
        await self.check_grace_period_expiry(now)

        # 3. KIỂM TRA PHIÊN HOÀN THÀNH HOẶC RỜI PHÒNG QUÁ HẠN
        await self.check_active_sessions_progress(now)

    async def send_dm_reminder(self, user_id: int, task_title: str):
        """Gửi tin nhắn riêng (DM) nhắc nhở trước 5 phút."""
        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if member:
                try:
                    embed = discord.Embed(
                        title="🔔 NHẮC NHỞ FOCUS 🔔",
                        description=f"Mục tiêu **{task_title}** của bạn sẽ bắt đầu sau **5 phút** nữa!\n\nHãy chuẩn bị vào phòng Voice để hệ thống ghi nhận.",
                        color=discord.Color.gold()
                    )
                    await member.send(embed=embed)
                    logger.info(f"Đã gửi nhắc nhở trước 5 phút cho User ID {user_id}.")
                except discord.Forbidden:
                    logger.warning(f"Không thể gửi DM nhắc nhở cho {member.name} (Chặn DM).")
                break

    async def handle_task_start_time(self, task: Task):
        """Xử lý sự kiện khi một mục tiêu đến giờ G."""
        user_id = task.user_id
        member = None
        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if member:
                break
        
        if not member:
            return

        now = datetime.datetime.now()

        # Kiểm tra xem user có đang ở trong kênh thoại nào không
        if member.voice and member.voice.channel:
            # Kiểm tra xem phòng voice hiện tại của user có khớp với loại task hay không
            channel_name = member.voice.channel.name.lower()
            required_type = None
            if "học tập" in channel_name or "học" in channel_name or member.voice.channel.id == config.PHONG_HOC_TAP_ID:
                required_type = "học tập"
            elif "làm việc" in channel_name or "work" in channel_name or member.voice.channel.id == config.PHONG_LAM_VIEC_ID:
                required_type = "làm việc"
            elif "giải trí" in channel_name or "chơi game" in channel_name or "game" in channel_name:
                required_type = "giải trí"

            # Nếu user đang ở trong phòng voice bị giới hạn và loại task không khớp
            if required_type is not None and task.task_type != required_type:
                wrong_channel_name = member.voice.channel.name
                
                try:
                    embed = discord.Embed(
                        title="⚠️ SAI PHÒNG VOICE ⚠️",
                        description=(
                            f"Đã đến giờ bắt đầu ca Focus cho mục tiêu **{task.title}** ({task.task_type}).\n\n"
                            f"Tuy nhiên, bạn đang ở sai phòng voice (**{wrong_channel_name}**). "
                            f"Bạn đã bị mời ra khỏi phòng voice đó.\n"
                            f"Bạn có **5 phút** để tham gia phòng Voice **{task.task_type}** tương ứng để điểm danh bắt đầu."
                        ),
                        color=discord.Color.orange()
                    )
                    await member.send(embed=embed)
                except discord.Forbidden:
                    pass

                # Ngắt kết nối user khỏi voice để đưa vào trạng thái Grace
                try:
                    await member.move_to(None)
                except Exception:
                    pass
                
                self.grace_sessions[user_id] = {
                    "task_id": task.task_id,
                    "start_time": now,
                    "duration": task.duration_minutes,
                    "task_title": task.title,
                    "task_type": task.task_type
                }
                return

            # Khởi tạo FocusSession trong DB nếu ở đúng phòng (hoặc phòng không bị giới hạn)
            async with get_db_session() as session:
                new_fs = FocusSession(
                    user_id=user_id,
                    task_id=task.task_id,
                    start_time=now,
                    status="pending"
                )
                session.add(new_fs)
                await session.flush()
                session_id = new_fs.session_id
                await session.commit()

            # Lưu vào bộ nhớ giám sát active
            self.active_sessions[user_id] = {
                "task_id": task.task_id,
                "session_id": session_id,
                "start_time": now,
                "duration": task.duration_minutes,
                "task_title": task.title,
                "voice_channel_id": member.voice.channel.id,
                "disconnect_time": None,
                "task_type": task.task_type,
                "original_duration": task.duration_minutes,
                "is_extended": False,
                "warning_sent": False
            }

            # Gửi tin nhắn khởi động vào kênh text-in-voice
            try:
                embed = discord.Embed(
                    title="⏰ BẮT ĐẦU PHIÊN TẬP TRUNG ⏰",
                    description=(
                        f"Chào {member.mention}! Ca làm việc tập trung cho mục tiêu **{task.title}** ({task.task_type}) đã chính thức bắt đầu!\n\n"
                        f"• **Thời lượng:** `{task.duration_minutes} phút`\n"
                        f"• **Hạn cuối:** `{(now + datetime.timedelta(minutes=task.duration_minutes)).strftime('%H:%M')}`\n\n"
                        f"💡 *Hãy ngồi lại trong phòng voice này cho đến khi hết giờ nhé.*"
                    ),
                    color=discord.Color.green(),
                    timestamp=now
                )
                await member.voice.channel.send(embed=embed)
            except Exception as e:
                logger.error(f"Không thể gửi tin nhắn bắt đầu Focus vào kênh voice: {e}")
        else:
            # Không ở trong phòng voice -> Cho 5 phút grace period
            self.grace_sessions[user_id] = {
                "task_id": task.task_id,
                "start_time": now,
                "duration": task.duration_minutes,
                "task_title": task.title,
                "task_type": task.task_type
            }
            try:
                embed = discord.Embed(
                    title="⚠️ CẢNH BÁO BẮT ĐẦU MUỘN ⚠️",
                    description=(
                        f"Đã đến giờ G cho mục tiêu **{task.title}** ({task.task_type})!\n\n"
                        f"Bạn có **5 phút** (Grace Period) để tham gia phòng Voice **{task.task_type}** để điểm danh bắt đầu. "
                        f"Nếu quá thời gian này, bạn sẽ bị tính là **Vi Phạm Kỷ Luật**."
                    ),
                    color=discord.Color.orange()
                )
                await member.send(embed=embed)
                logger.info(f"Đã gửi cảnh báo muộn 5 phút cho {member.name}.")
            except discord.Forbidden:
                pass

    async def check_grace_period_expiry(self, now: datetime.datetime):
        """Kiểm tra xem các user trong grace period đã quá 5 phút chưa."""
        for user_id in list(self.grace_sessions.keys()):
            grace_session = self.grace_sessions[user_id]
            elapsed_seconds = (now - grace_session["start_time"]).total_seconds()
            
            # Quá 5 phút
            if elapsed_seconds >= 300:
                logger.warning(f"User ID {user_id} vắng mặt quá 5 phút grace period.")
                task_title = grace_session["task_title"]
                
                # Kiểm tra Thẻ Nghỉ Phép (rest_token) cứu cánh
                used_rest_token = False
                async with get_db_session() as session:
                    inv_res = await session.execute(
                        select(Inventory).filter_by(user_id=user_id, item_id="rest_token")
                    )
                    inv = inv_res.scalar_one_or_none()
                    
                    if inv and inv.quantity > 0:
                        # Tiêu hao 1 Thẻ Nghỉ Phép
                        if inv.quantity > 1:
                            inv.quantity -= 1
                        else:
                            await session.delete(inv)
                        
                        # Tạo bản ghi Session kết thúc (hoặc được miễn giảm) mà không phạt
                        new_fs = FocusSession(
                            user_id=user_id,
                            task_id=grace_session["task_id"],
                            start_time=grace_session["start_time"],
                            end_time=now,
                            actual_duration=0,
                            status="failed" # Ghi nhận phiên thất bại nhưng được bảo toàn streak
                        )
                        session.add(new_fs)
                        
                        # Cập nhật ngày tập trung cuối cùng về ngày hôm nay để bảo tồn chuỗi streak!
                        user_res = await session.execute(select(User).filter_by(user_id=user_id))
                        user = user_res.scalar_one()
                        user.last_focus_date = now.date()
                        
                        await session.commit()
                        used_rest_token = True
                        logger.info(f"User ID {user_id} đã tự động kích hoạt Thẻ Nghỉ Phép. Streak được bảo toàn.")
                
                # Xóa khỏi danh sách grace
                self.grace_sessions.pop(user_id)

                if used_rest_token:
                    # Gửi tin nhắn thông báo kích hoạt Thẻ Nghỉ Phép thành công
                    member = None
                    for guild in self.bot.guilds:
                        member = guild.get_member(user_id)
                        if member:
                            break
                    if member:
                        try:
                            embed = discord.Embed(
                                title="🎫 THẺ NGHỈ PHÉP ĐÃ KÍCH HOẠT 🎫",
                                description=(
                                    f"Chào bạn, bạn đã vắng mặt trong ca Focus cho mục tiêu **{task_title}**.\n\n"
                                    f"Hệ thống đã **tự động tiêu thụ 1 Thẻ Nghỉ Phép** trong túi đồ của bạn. "
                                    f"Chuỗi Streak kỷ luật của bạn đã được **bảo vệ thành công**! 🔒"
                                ),
                                color=discord.Color.blue()
                            )
                            await member.send(embed=embed)
                        except discord.Forbidden:
                            pass
                else:
                    # Không có Thẻ Nghỉ Phép -> Thực thi phạt bình thường
                    # 1. Ghi nhận Session thất bại vào DB
                    async with get_db_session() as session:
                        new_fs = FocusSession(
                            user_id=user_id,
                            task_id=grace_session["task_id"],
                            start_time=grace_session["start_time"],
                            end_time=now,
                            actual_duration=0,
                            status="failed"
                        )
                        session.add(new_fs)
                        await session.commit()

                    # 2. Kích hoạt hình phạt từ Module 4
                    punishment_cog = self.bot.get_cog("Punishment")
                    if punishment_cog:
                        guild_id = self.bot.guilds[0].id if self.bot.guilds else 0
                        for guild in self.bot.guilds:
                            if guild.get_member(user_id):
                                guild_id = guild.id
                                break
                        await punishment_cog.apply_punishment(
                            user_id,
                            guild_id,
                            f"Vắng mặt quá 5 phút kể từ giờ G của mục tiêu '{task_title}'."
                        )

    async def check_active_sessions_progress(self, now: datetime.datetime):
        """Kiểm tra tiến trình các phiên Focus đang hoạt động (Hoàn thành hoặc Rời voice quá lâu)."""
        # C. Quét và gửi cảnh báo trước 5 phút khi phiên làm việc sắp hết giờ
        for user_id, session_info in list(self.active_sessions.items()):
            if session_info.get("warning_sent"):
                continue
            
            # Tính thời gian còn lại (phút)
            start_time = session_info["start_time"]
            duration = session_info["duration"]
            elapsed_minutes = (now - start_time).total_seconds() / 60
            remaining_minutes = duration - elapsed_minutes
            
            # Gửi cảnh báo nếu còn khoảng 5 phút trở xuống
            if 0.0 <= remaining_minutes <= 5.0:
                voice_channel_id = session_info.get("voice_channel_id")
                if not voice_channel_id:
                    continue
                
                # Gom nhóm tất cả người dùng trong cùng phòng voice này có phiên sắp kết thúc trong 5 phút và chưa được cảnh báo
                group_user_ids = []
                for u_id, s_info in self.active_sessions.items():
                    if s_info.get("voice_channel_id") == voice_channel_id:
                        u_start = s_info["start_time"]
                        u_dur = s_info["duration"]
                        u_rem = u_dur - (now - u_start).total_seconds() / 60
                        if u_rem <= 5.0 and not s_info.get("warning_sent"):
                            group_user_ids.append(u_id)
                
                if not group_user_ids:
                    continue
                
                # Đánh dấu đã gửi cảnh báo cho cả nhóm để tránh gửi lặp lại
                for u_id in group_user_ids:
                    self.active_sessions[u_id]["warning_sent"] = True
                
                # Lấy kênh thoại
                channel = self.bot.get_channel(voice_channel_id)
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(voice_channel_id)
                    except Exception:
                        pass
                
                if channel and isinstance(channel, discord.VoiceChannel):
                    mentions = []
                    for u_id in group_user_ids:
                        guild_member = channel.guild.get_member(u_id)
                        if guild_member:
                            mentions.append(guild_member.mention)
                    
                    if mentions:
                        embed = discord.Embed(
                            title="⏰ PHIÊN LÀM VIỆC SẮP KẾT THÚC! ⏰",
                            description=(
                                f"Chào các bạn {', '.join(mentions)}!\n\n"
                                f"Phiên tập trung hiện tại của phòng sắp hết giờ (còn dưới **5 phút** nữa).\n\n"
                                f"Hãy chọn một trong hai lựa chọn dưới đây:\n"
                                f"1. **Tôi đã hiểu**: Hệ thống sẽ tự động kick bạn khỏi phòng thoại khi hết giờ.\n"
                                f"2. **Cà phê đen đá**: Tiêu tốn **1 Cà Phê Đen Đá** trong túi đồ của bạn để gia hạn thêm **30 phút** cho cả phòng."
                            ),
                            color=discord.Color.gold()
                        )
                        view = SessionWarningView(self, group_user_ids, voice_channel_id)
                        try:
                            msg = await channel.send(embed=embed, view=view)
                            view.message = msg
                            logger.info(f"Đã gửi cảnh báo 5 phút cho nhóm {group_user_ids} tại phòng voice ID {voice_channel_id}")
                        except Exception as e:
                            logger.error(f"Lỗi khi gửi cảnh báo 5 phút đến phòng voice: {e}")

        # Kiểm tra tiến trình ngắt kết nối và hoàn thành bình thường
        for user_id in list(self.active_sessions.keys()):
            session_info = self.active_sessions[user_id]
            
            # A. Kiểm tra nếu user bị rớt kết nối (Mất voice) quá 1 phút
            if session_info["disconnect_time"] is not None:
                disconnected_seconds = (now - session_info["disconnect_time"]).total_seconds()
                if disconnected_seconds >= 60:
                    logger.warning(f"User ID {user_id} tự ý rời voice quá 1 phút.")
                    task_title = session_info["task_title"]
                    session_id = session_info["session_id"]
                    
                    # Kiểm tra xem có đang ở thời gian gia hạn hay không
                    elapsed_minutes = (now - session_info["start_time"]).total_seconds() / 60
                    if session_info.get("is_extended") and elapsed_minutes >= session_info.get("original_duration", 0):
                        logger.info(f"User ID {user_id} tự ý rời voice trong thời gian gia hạn. Hoàn thành phiên thành công không phạt.")
                        # Thời gian thực tế được ghi nhận là thời gian trôi qua trừ đi 1 phút disconnect
                        actual_duration = max(session_info.get("original_duration", 0), int(elapsed_minutes - 1))
                        session_info["duration"] = actual_duration
                        await self.complete_focus_session(user_id, session_id, session_info, now)
                        self.active_sessions.pop(user_id)
                        continue

                    # 1. Cập nhật Session thất bại trong DB
                    async with get_db_session() as session:
                        await session.execute(
                            update(FocusSession)
                            .where(FocusSession.session_id == session_id)
                            .values(status="failed", end_time=now, actual_duration=0)
                        )
                        await session.commit()
                    
                    self.active_sessions.pop(user_id)

                    # 2. Phạt người dùng
                    punishment_cog = self.bot.get_cog("Punishment")
                    if punishment_cog:
                        guild_id = self.bot.guilds[0].id if self.bot.guilds else 0
                        for guild in self.bot.guilds:
                            if guild.get_member(user_id):
                                guild_id = guild.id
                                break
                        await punishment_cog.apply_punishment(
                            user_id,
                            guild_id,
                            f"Tự ý rời phòng voice quá 1 phút trong ca Focus '{task_title}'."
                        )
                    continue

            # B. Kiểm tra hoàn thành phiên làm việc thành công
            start_time = session_info["start_time"]
            duration = session_info["duration"]
            if (now - start_time).total_seconds() / 60 >= duration:
                # Hoàn thành ca Focus thành công!
                await self.complete_focus_session(user_id, session_info["session_id"], session_info, now)
                self.active_sessions.pop(user_id)

    async def complete_focus_session(self, user_id: int, session_id: int, session_info, now: datetime.datetime):
        """Xử lý ghi nhận hoàn thành phiên Focus thành công và phát thưởng."""
        if isinstance(session_info, dict):
            task_title = session_info.get("task_title", session_info.get("title", "Mục tiêu"))
            duration = session_info.get("duration", 0)
        else:
            task_title = getattr(session_info, "title", "Mục tiêu")
            duration = getattr(session_info, "duration_minutes", 0)
        
        # Tính toán phần thưởng
        earned_xp = duration * 2
        earned_tokens = duration // 25
        today_date = now.date()
        is_x2_active = False

        async with get_db_session() as session:
            # 1. Cập nhật session thành hoàn thành (completed)
            await session.execute(
                update(FocusSession)
                .where(FocusSession.session_id == session_id)
                .values(status="completed", end_time=now, actual_duration=duration)
            )

            # 2. Cập nhật chỉ số cho User
            user_res = await session.execute(select(User).filter_by(user_id=user_id))
            user = user_res.scalar_one()

            # Kiểm tra Thẻ X2 Tốc Độ còn hạn không
            if user.x2_expiry and now < user.x2_expiry:
                earned_xp *= 2
                earned_tokens *= 2
                is_x2_active = True

            old_level = user.level
            user.exp += earned_xp
            user.token_balance += earned_tokens
            
            # Cập nhật cấp độ
            user.level = int(math.sqrt(user.exp) / 10) + 1
            
            # Tính toán Streak kỷ luật hằng ngày
            if user.last_focus_date is None:
                user.current_streak = 1
            elif user.last_focus_date == today_date:
                # Đã làm việc hôm nay, giữ nguyên chuỗi
                pass
            elif user.last_focus_date == today_date - datetime.timedelta(days=1):
                # Ngày tập trung cuối là hôm qua -> Tăng streak
                user.current_streak += 1
            else:
                # Bị đứt chuỗi
                user.current_streak = 1

            user.max_streak = max(user.max_streak, user.current_streak)
            user.last_focus_date = today_date

            await session.commit()
            
        logger.info(f"User ID {user_id} hoàn thành Focus '{task_title}' nhận {earned_xp} XP, {earned_tokens} Tokens. Streak: {user.current_streak} (X2={is_x2_active})")

        # 3. Gửi thông báo chúc mừng trên Discord
        member = None
        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if member:
                break
                
        if member:
            embed = discord.Embed(
                title="🎉 HOÀN THÀNH CA FOCUS THÀNH CÔNG! 🎉",
                description=(
                    f"Chúc mừng {member.mention} đã hoàn thành xuất sắc ca Focus mục tiêu **{task_title}**!\n\n"
                    f"🎁 **Phần thưởng tích lũy:**\n"
                    f"• **Kinh nghiệm:** `+{earned_xp} XP` (Cấp hiện tại: **Cấp {user.level}**)\n"
                    f"• **Tokens:** `+{earned_tokens} Tokens` 🪙\n"
                    f"• **Chuỗi Streak kỷ luật:** `{user.current_streak} ngày liên tục` 🔥"
                ),
                color=discord.Color.gold(),
                timestamp=now
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            
            if is_x2_active:
                embed.description += f"\n\n⚡ **Thẻ X2 Tốc Độ đang kích hoạt!** Bạn nhận gấp đôi EXP và Tokens!"

            # Thông báo lên level mới nếu có
            if user.level > old_level:
                embed.description += f"\n\n🚀 **THĂNG CẤP MỚI!** Bạn đã đạt **Cấp {user.level}**!"

            # Thử gửi vào kênh voice của họ trước, nếu không được thì gửi DM
            # Thử gửi vào kênh voice của họ trước, nếu không được thì gửi DM
            sent = False
            voice_channel = None
            if member.voice and member.voice.channel:
                voice_channel = member.voice.channel
                try:
                    await voice_channel.send(embed=embed)
                    sent = True
                except Exception:
                    pass
            
            if not sent:
                try:
                    await member.send(embed=embed)
                except discord.Forbidden:
                    pass

            # Tự động kick ra khỏi phòng voice khi hết phiên làm việc
            if voice_channel:
                try:
                    await member.move_to(None, reason="Phiên làm việc kết thúc")
                    logger.info(f"Đã kick {member.display_name} khỏi phòng voice do phiên làm việc kết thúc.")
                except Exception as e:
                    logger.error(f"Lỗi khi di chuyển/kick {member.display_name} ra khỏi phòng voice: {e}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """
        Lắng nghe sự kiện voice state update để:
        1. Nhận diện khi người đang trong Grace Period vào phòng voice kịp lúc để kích hoạt Focus.
        2. Nhận diện khi người đang Focus thoát phòng voice để bắt đầu tính thời gian Rejoin Grace (1 phút).
        """
        if member.bot:
            return

        user_id = member.id
        now = datetime.datetime.now()

        # Kiểm tra nếu người dùng vào phòng voice bị giới hạn
        if after.channel is not None:
            # Bỏ qua nếu người dùng vào phòng trigger để tạo phòng Focus động
            if after.channel.id == config.FOCUS_TRIGGER_CHANNEL_ID:
                return
                
            channel_name = after.channel.name.lower()
            required_type = None
            if "học tập" in channel_name or "học" in channel_name or after.channel.id == config.PHONG_HOC_TAP_ID:
                required_type = "học tập"
            elif "làm việc" in channel_name or "work" in channel_name or after.channel.id == config.PHONG_LAM_VIEC_ID:
                required_type = "làm việc"
            elif "giải trí" in channel_name or "chơi game" in channel_name or "game" in channel_name:
                required_type = "giải trí"

            if required_type is not None:
                has_valid_session = False
                
                # Check active sessions
                if user_id in self.active_sessions:
                    session_info = self.active_sessions[user_id]
                    if session_info.get("task_type") == required_type:
                        has_valid_session = True
                
                # Check grace sessions
                elif user_id in self.grace_sessions:
                    grace_info = self.grace_sessions[user_id]
                    if grace_info.get("task_type") == required_type:
                        has_valid_session = True
                        
                if not has_valid_session:
                    channel_name_str = after.channel.name
                    
                    # Send DM to explain why they were kicked
                    try:
                        embed = discord.Embed(
                            title="❌ KHÔNG THỂ VÀO PHÒNG VOICE ❌",
                            description=(
                                f"Bạn không thể tham gia phòng voice **{channel_name_str}** lúc này.\n\n"
                                f"Để vào phòng này, bạn cần đăng ký mục tiêu tập trung có loại là **{required_type}** trước."
                            ),
                            color=discord.Color.red()
                        )
                        await member.send(embed=embed)
                    except discord.Forbidden:
                        pass

                    # Kick the user from voice channel!
                    try:
                        await member.move_to(None)
                    except Exception:
                        pass
                    return  # Stop processing this voice state update

        # TRƯỜNG HỢP 1: Người dùng đang trong thời gian chờ Grace Period (5 phút) và tham gia phòng Voice kịp lúc
        if user_id in self.grace_sessions and after.channel is not None:
            grace_info = self.grace_sessions.pop(user_id)
            logger.info(f"User ID {user_id} đã tham gia voice kịp thời trước hạn grace period.")
            
            # Bắt đầu FocusSession chính thức trong DB
            async with get_db_session() as session:
                new_fs = FocusSession(
                    user_id=user_id,
                    task_id=grace_info["task_id"],
                    start_time=now,
                    status="pending"
                )
                session.add(new_fs)
                await session.flush()
                session_id = new_fs.session_id
                await session.commit()

            # Chuyển vào active sessions
            self.active_sessions[user_id] = {
                "task_id": grace_info["task_id"],
                "session_id": session_id,
                "start_time": now,
                "duration": grace_info["duration"],
                "task_title": grace_info["task_title"],
                "voice_channel_id": after.channel.id,
                "disconnect_time": None,
                "task_type": grace_info["task_type"],
                "original_duration": grace_info["duration"],
                "is_extended": False,
                "warning_sent": False
            }

            # Gửi tin nhắn bắt đầu tới phòng voice
            try:
                embed = discord.Embed(
                    title="⏰ BẮT ĐẦU CA FOCUS TRỄ ⏰",
                    description=(
                        f"Chào {member.mention}! Bạn đã có mặt kịp thời trước khi bị phạt.\n"
                        f"Ca Focus cho mục tiêu **{grace_info['task_title']}** ({grace_info['task_type']}) chính thức được ghi nhận bắt đầu!\n\n"
                        f"• **Thời lượng:** `{grace_info['duration']} phút`"
                    ),
                    color=discord.Color.green(),
                    timestamp=now
                )
                await after.channel.send(embed=embed)
            except Exception:
                pass

        # TRƯỜNG HỢP 2: Người dùng đang Focus thoát phòng voice
        elif user_id in self.active_sessions:
            session_info = self.active_sessions[user_id]
            
            # User ngắt kết nối hoàn toàn hoặc di chuyển khỏi phòng voice được theo dõi
            is_disconnected = (after.channel is None)
            
            if is_disconnected:
                # Nếu họ chưa có thời gian rớt kết nối trước đó, ghi nhận thời gian bắt đầu rớt
                if session_info["disconnect_time"] is None:
                    session_info["disconnect_time"] = now
                    logger.info(f"Thành viên {member.display_name} rớt khỏi phòng voice trong khi đang Focus. Bắt đầu đếm ngược 1 phút rejoin.")
                    try:
                        embed = discord.Embed(
                            title="⚠️ CẢNH BÁO RỜI PHÒNG VOICE ⚠️",
                            description=(
                                f"Bạn vừa rời khỏi phòng voice trong khi ca Focus **{session_info['task_title']}** đang diễn ra!\n\n"
                                f"Bạn có **1 phút** để kết nối lại vào bất kỳ kênh thoại nào để tiếp tục ca làm việc, "
                                f"nếu không phiên của bạn sẽ bị hủy và bị **phạt vi phạm**."
                            ),
                            color=discord.Color.red()
                        )
                        await member.send(embed=embed)
                    except discord.Forbidden:
                        pass
            else:
                # Nếu họ quay lại phòng voice thành công
                if session_info["disconnect_time"] is not None:
                    session_info["disconnect_time"] = None
                    logger.info(f"Thành viên {member.display_name} đã quay trở lại phòng voice kịp thời.")
                    try:
                        await member.send("✅ **Kết nối thành công!** Phiên làm việc Focus của bạn tiếp tục được duy trì.")
                    except discord.Forbidden:
                        pass

async def setup(bot: commands.Bot):
    await bot.add_cog(Tracker(bot))
