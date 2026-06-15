import logging
import re
import datetime
import discord
from discord.ext import commands, tasks
from discord import app_commands
from sqlalchemy import select
from database.db_session import get_db_session
from database.models import User, Task, TaskInvite, FocusSession

logger = logging.getLogger("ChronosBot.ControlPanel")

async def parse_collaborators(bot: discord.Client, guild: discord.Guild, input_str: str) -> list[discord.User]:
    if not input_str:
        return []
    
    parts = [p.strip() for p in input_str.split(",") if p.strip()]
    resolved_users = []
    
    for part in parts:
        # Check for mention or numeric ID
        match = re.match(r"^<@!?(\d+)>$", part)
        user_id = None
        if match:
            user_id = int(match.group(1))
        elif part.isdigit():
            user_id = int(part)
        
        if user_id:
            user = None
            if guild:
                user = guild.get_member(user_id)
                if not user:
                    try:
                        user = await guild.fetch_member(user_id)
                    except discord.HTTPException:
                        pass
            if not user:
                try:
                    user = await bot.fetch_user(user_id)
                except discord.HTTPException:
                    pass
            if user:
                resolved_users.append(user)
        else:
            # Match by name, nick, global name
            part_clean = part.lstrip("@").lower()
            found_user = None
            if guild:
                for member in guild.members:
                    if (member.name.lower() == part_clean or 
                        (member.global_name and member.global_name.lower() == part_clean) or
                        (member.nick and member.nick.lower() == part_clean)):
                        found_user = member
                        break
            if not found_user:
                for user in bot.users:
                    if user.name.lower() == part_clean or (user.global_name and user.global_name.lower() == part_clean):
                        found_user = user
                        break
            if found_user:
                resolved_users.append(found_user)
                
    unique_users = []
    seen = set()
    for u in resolved_users:
        if u.id not in seen and u.id != bot.user.id:
            seen.add(u.id)
            unique_users.append(u)
            
    return unique_users


def tasks_overlap(t1_start: datetime.time, t1_dur: int, t1_date: datetime.date,
                  t2_start: datetime.time, t2_dur: int, t2_date: datetime.date) -> bool:
    """
    Kiểm tra hai Task có bị trùng lặp thời gian hoạt động với nhau hay không.
    Vì các Task chỉ chạy một lần đúng ngày start_date, chúng chỉ có thể trùng lịch nếu cùng ngày và trùng giờ.
    """
    if t1_date and t2_date and t1_date != t2_date:
        return False

    t1_start_min = t1_start.hour * 60 + t1_start.minute
    t2_start_min = t2_start.hour * 60 + t2_start.minute
    
    # Hàm chia nhỏ khoảng thời gian khi nó vắt qua nửa đêm (ngày hôm sau)
    def get_day_intervals(start: int, duration: int):
        intervals = []
        end = start + duration
        if end > 1440:
            intervals.append((start, 1440))
            intervals.append((0, end - 1440))
        else:
            intervals.append((start, end))
        return intervals

    intervals1 = get_day_intervals(t1_start_min, t1_dur)
    intervals2 = get_day_intervals(t2_start_min, t2_dur)
    
    for s1, e1 in intervals1:
        for s2, e2 in intervals2:
            if max(s1, s2) < min(e1, e2):
                return True
    return False


async def get_active_and_upcoming_tasks(session, user_id: int) -> list[Task]:
    res = await session.execute(
        select(Task).filter_by(user_id=user_id).order_by(Task.start_time)
    )
    all_tasks = res.scalars().all()
    
    now = datetime.datetime.now()
    today = now.date()
    
    filtered = []
    for task in all_tasks:
        # 1. Upcoming start date is always shown
        if task.start_date > today:
            filtered.append(task)
            continue
            
        # 2. Only scheduled today if start_date == today
        if task.start_date == today:
            # Check if there is a completed or failed session today
            start_of_day = datetime.datetime.combine(today, datetime.time.min)
            end_of_day = datetime.datetime.combine(today, datetime.time.max)
            
            fs_res = await session.execute(
                select(FocusSession)
                .where(
                    FocusSession.task_id == task.task_id,
                    FocusSession.start_time >= start_of_day,
                    FocusSession.start_time <= end_of_day,
                    FocusSession.status.in_(["completed", "failed"])
                )
            )
            today_sessions = fs_res.scalars().all()
            if today_sessions:
                # Already processed (completed/failed) today -> in the past/done
                continue
                
            # Check if session has ended for today
            task_start_dt = datetime.datetime.combine(today, task.start_time)
            task_end_dt = task_start_dt + datetime.timedelta(minutes=task.duration_minutes)
            if now >= task_end_dt:
                # Occurrence for today is in the past
                continue
                
            # Otherwise, active or upcoming today
            filtered.append(task)
                
    return filtered


async def disable_view_buttons(message: discord.Message, status_text: str = None):
    if not message or not message.embeds:
        return
    embed = message.embeds[0]
    if status_text:
        embed.description = (embed.description or "") + f"\n\n**Trạng thái:** {status_text}"
        if "đồng ý" in status_text.lower():
            embed.color = discord.Color.green()
        elif "từ chối" in status_text.lower():
            embed.color = discord.Color.red()
        else:
            embed.color = discord.Color.light_grey()
    try:
        await message.edit(embed=embed, view=None)
    except Exception as e:
        logger.error(f"Lỗi khi edit message để tắt nút bấm: {e}")


class TaskInviteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Đồng ý ✅",
        style=discord.ButtonStyle.green,
        custom_id="chronos_btn_invite_accept"
    )
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        message_id = interaction.message.id

        try:
            async with get_db_session() as session:
                # Find the pending invite
                res = await session.execute(
                    select(TaskInvite).filter_by(message_id=message_id, invitee_id=user_id, status="pending")
                )
                invite = res.scalar_one_or_none()
                if not invite:
                    await interaction.followup.send("❌ Lời mời này đã được xử lý hoặc không hợp lệ.", ephemeral=True)
                    await disable_view_buttons(interaction.message)
                    return

                # Get original task
                task_res = await session.execute(
                    select(Task).filter_by(task_id=invite.task_id)
                )
                original_task = task_res.scalar_one_or_none()
                if not original_task:
                    await interaction.followup.send("❌ Mục tiêu gốc đã bị xóa.", ephemeral=True)
                    invite.status = "declined"
                    await session.commit()
                    await disable_view_buttons(interaction.message, status_text="Mục tiêu gốc đã bị xóa")
                    return

                # Check if invitee has any overlapping tasks
                invitee_tasks_res = await session.execute(
                    select(Task).filter_by(user_id=user_id)
                )
                invitee_tasks = invitee_tasks_res.scalars().all()
                
                overlapping_task = None
                for t in invitee_tasks:
                    if tasks_overlap(
                        original_task.start_time, original_task.duration_minutes, original_task.start_date,
                        t.start_time, t.duration_minutes, t.start_date
                    ):
                        overlapping_task = t
                        break
                
                if overlapping_task:
                    invite.status = "declined"
                    await session.commit()
                    
                    date_str = overlapping_task.start_date.strftime("%d/%m/%Y") if overlapping_task.start_date else ""
                    dummy_dt = datetime.datetime.combine(datetime.date.today(), overlapping_task.start_time)
                    end_dt = dummy_dt + datetime.timedelta(minutes=overlapping_task.duration_minutes)
                    end_str = end_dt.strftime("%H:%M")
                    end_suffix = " (ngày hôm sau)" if end_dt.date() > dummy_dt.date() else ""
                    time_range_str = f"{date_str + ' ' if date_str else ''}{overlapping_task.start_time.strftime('%H:%M')} - {end_str}{end_suffix}"
                    
                    # Respond to invitee
                    await interaction.followup.send(
                        f"❌ **Không thể tham gia mục tiêu này!** Phiên làm việc không thể tạo vì chồng thời gian với phiên làm việc đã đăng ký trước đó của bạn:\n"
                        f"• Mục tiêu trùng: `{overlapping_task.title}` (`{time_range_str}`)",
                        ephemeral=True
                    )
                    await disable_view_buttons(interaction.message, status_text="Không thể tham gia (Trùng lịch) ❌")
                    
                    # Notify creator
                    creator_id = original_task.user_id
                    creator = interaction.client.get_user(creator_id)
                    if not creator:
                        try:
                            creator = await interaction.client.fetch_user(creator_id)
                        except Exception:
                            pass
                    if creator:
                        try:
                            await creator.send(
                                f"❌ **{interaction.user.mention}** đã đồng ý tham gia mục tiêu tập trung `{original_task.title}` cùng bạn, "
                                f"nhưng không thể tham gia vì bị trùng thời gian với mục tiêu `{overlapping_task.title}` (`{time_range_str}`) đã đăng ký trước đó của họ."
                            )
                        except Exception as e:
                            logger.error(f"Không thể gửi thông báo tới creator khi trùng lịch: {e}")
                    return

                # Check if invitee exists in users table, if not, create
                invitee_res = await session.execute(
                    select(User).filter_by(user_id=user_id)
                )
                invitee_user = invitee_res.scalar_one_or_none()
                if not invitee_user:
                    invitee_user = User(
                        user_id=user_id, 
                        exp=0, 
                        level=1, 
                        token_balance=0,
                        current_streak=0,
                        max_streak=0
                    )
                    session.add(invitee_user)
                    await session.flush()

                # Duplicate the task for the invitee
                new_task = Task(
                    user_id=user_id,
                    title=original_task.title,
                    start_time=original_task.start_time,
                    duration_minutes=original_task.duration_minutes,
                    start_date=original_task.start_date,
                    task_type=original_task.task_type
                )
                session.add(new_task)
                
                # Update invite status
                invite.status = "accepted"
                await session.commit()

            # Notify creator
            creator_id = original_task.user_id
            creator = interaction.client.get_user(creator_id)
            if not creator:
                try:
                    creator = await interaction.client.fetch_user(creator_id)
                except Exception:
                    pass
            if creator:
                try:
                    await creator.send(
                        f"✅ **{interaction.user.mention}** đã **đồng ý** tham gia mục tiêu tập trung `{original_task.title}` cùng bạn!"
                    )
                except Exception as e:
                    logger.error(f"Không thể gửi thông báo tới creator: {e}")

            # Respond to invitee and update the message
            response_msg = (
                f"✅ **Bạn đã đồng ý tham gia mục tiêu tập trung thành công!**\n"
                f"• **Mục tiêu:** `{original_task.title}`\n"
                f"• **Loại mục tiêu:** `{original_task.task_type}`\n"
                f"• **Ngày bắt đầu:** `{original_task.start_date.strftime('%d/%m/%Y') if original_task.start_date else ''}`\n"
                f"• **Thời gian bắt đầu:** `{original_task.start_time.strftime('%H:%M') if original_task.start_time else ''}`\n"
                f"• **Thời lượng:** `{original_task.duration_minutes} phút`\n"
            )
            response_msg += f"\n🔔 *Bot sẽ tự động nhắc nhở trước giờ học/làm việc 5 phút.*"
            response_msg += f"\n🔊 *Khi đến giờ hãy vào phòng {original_task.task_type}.*"

            await interaction.followup.send(response_msg, ephemeral=True)
            await disable_view_buttons(interaction.message, status_text="Đã đồng ý ✅")

        except Exception as e:
            logger.error(f"Lỗi khi xử lý đồng ý lời mời: {e}")
            await interaction.followup.send("❌ Lỗi hệ thống khi xử lý lời mời.", ephemeral=True)

    @discord.ui.button(
        label="Từ chối ❌",
        style=discord.ButtonStyle.red,
        custom_id="chronos_btn_invite_decline"
    )
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        message_id = interaction.message.id

        try:
            async with get_db_session() as session:
                # Find the pending invite
                res = await session.execute(
                    select(TaskInvite).filter_by(message_id=message_id, invitee_id=user_id, status="pending")
                )
                invite = res.scalar_one_or_none()
                if not invite:
                    await interaction.followup.send("❌ Lời mời này đã được xử lý hoặc không hợp lệ.", ephemeral=True)
                    await disable_view_buttons(interaction.message)
                    return

                # Get original task
                task_res = await session.execute(
                    select(Task).filter_by(task_id=invite.task_id)
                )
                original_task = task_res.scalar_one_or_none()
                
                # Update invite status
                invite.status = "declined"
                await session.commit()

            # Notify creator
            if original_task:
                creator_id = original_task.user_id
                creator = interaction.client.get_user(creator_id)
                if not creator:
                    try:
                        creator = await interaction.client.fetch_user(creator_id)
                    except Exception:
                        pass
                if creator:
                    try:
                        await creator.send(
                            f"❌ **{interaction.user.mention}** đã **từ chối** tham gia mục tiêu tập trung `{original_task.title}` cùng bạn."
                        )
                    except Exception as e:
                        logger.error(f"Không thể gửi thông báo tới creator: {e}")

            # Respond to invitee and update the message
            await interaction.followup.send("❌ Bạn đã từ chối tham gia mục tiêu tập trung.", ephemeral=True)
            await disable_view_buttons(interaction.message, status_text="Đã từ chối ❌")

        except Exception as e:
            logger.error(f"Lỗi khi xử lý từ chối lời mời: {e}")
            await interaction.followup.send("❌ Lỗi hệ thống khi xử lý lời mời.", ephemeral=True)


class TaskRegistrationModal(discord.ui.Modal):
    """
    Discord Modal hiển thị hộp thoại pop-up cho phép user nhập thông tin đăng ký mục tiêu.
    """
    def __init__(self, task_type: str):
        super().__init__(title=f"Đăng Ký Mục Tiêu ({task_type})")
        self.task_type = task_type
    task_name = discord.ui.TextInput(
        label="Tên mục tiêu (Task)",
        placeholder="Ví dụ: Code game, Học tiếng Anh, Làm báo cáo...",
        required=True,
        max_length=100
    )
    start_date = discord.ui.TextInput(
        label="Ngày bắt đầu (Định dạng DD/MM/YYYY)",
        placeholder="Ví dụ: 14/06/2026...",
        required=True,
        min_length=10,
        max_length=10
    )
    start_time = discord.ui.TextInput(
        label="Thời gian bắt đầu (Định dạng HH:MM)",
        placeholder="Ví dụ: 08:30, 20:00, 22:15...",
        required=True,
        min_length=5,
        max_length=5
    )
    duration = discord.ui.TextInput(
        label="Thời lượng tập trung (Phút)",
        placeholder="Ví dụ: 25, 50, 60, 90...",
        required=True,
        max_length=4
    )
    collaborators = discord.ui.TextInput(
        label="Làm việc cùng (Nhập Username/ID/Mention)",
        placeholder="Ví dụ: @user1, user2, 123456789 (ngăn cách bằng dấu phẩy)",
        required=False,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        # 1. Kiểm tra định dạng ngày bắt đầu
        now = datetime.datetime.now()
        try:
            parsed_date = datetime.datetime.strptime(self.start_date.value, "%d/%m/%Y").date()
            if parsed_date < now.date():
                await interaction.response.send_message(
                    "❌ **Ngày bắt đầu không được ở trong quá khứ!**\n"
                    "Vui lòng nhập ngày hôm nay hoặc một ngày trong tương lai.",
                    ephemeral=True
                )
                return
        except ValueError:
            await interaction.response.send_message(
                "❌ **Định dạng ngày bắt đầu không hợp lệ!**\n"
                "Vui lòng nhập đúng kiểu `DD/MM/YYYY` (Ví dụ: `14/06/2026`).",
                ephemeral=True
            )
            return

        # 2. Kiểm tra định dạng thời gian bắt đầu (Regex kiểm tra HH:MM từ 00:00 đến 23:59)
        time_match = re.match(r"^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$", self.start_time.value)
        if not time_match:
            await interaction.response.send_message(
                "❌ **Định dạng thời gian không hợp lệ!**\n"
                "Vui lòng nhập đúng kiểu `HH:MM` trong khoảng từ `00:00` tới `23:59` (Ví dụ: `09:15`, `20:00`).",
                ephemeral=True
            )
            return

        # Phân tích chuỗi thời gian thành đối tượng datetime.time
        hour, minute = map(int, self.start_time.value.split(":"))
        parsed_time = datetime.time(hour, minute)

        # Kiểm tra nếu ngày bắt đầu là hôm nay, thời gian bắt đầu không được ở trong quá khứ
        if parsed_date == now.date():
            current_time = datetime.time(now.hour, now.minute)
            if parsed_time < current_time:
                await interaction.response.send_message(
                    "❌ **Thời gian bắt đầu không được ở trong quá khứ!**\n"
                    "Vui lòng nhập giờ bắt đầu từ thời điểm hiện tại trở đi.",
                    ephemeral=True
                )
                return

        # 3. Kiểm tra định dạng thời lượng (phải là số nguyên và tối thiểu là 25 phút)
        try:
            duration_minutes = int(self.duration.value)
            if duration_minutes < 25:
                await interaction.response.send_message(
                    "❌ **Thời lượng tập trung tối thiểu là 25 phút!**\n"
                    "Vui lòng nhập số phút tập trung từ 25 trở lên (Ví dụ: `25`, `50`, `60`, `90`).",
                    ephemeral=True
                )
                return
        except ValueError:
            await interaction.response.send_message(
                "❌ **Thời lượng không hợp lệ!**\n"
                "Vui lòng nhập một số nguyên đại diện cho số phút tập trung từ 25 trở lên (Ví dụ: `25`, `60`, `90`).",
                ephemeral=True
            )
            return

        # Defer phản hồi để tránh timeout do phân tích user và gửi DM
        await interaction.response.defer(ephemeral=True)

        # 5. Phân tích danh sách người dùng được mời
        collaborators = []
        if self.collaborators.value:
            collaborators = await parse_collaborators(interaction.client, interaction.guild, self.collaborators.value)

        # 6. Ghi nhận thông tin vào Cơ sở dữ liệu
        user_id = interaction.user.id
        try:
            async with get_db_session() as session:
                # Tìm kiếm User trong DB, nếu chưa tồn tại thì tạo mới
                user_res = await session.execute(
                    select(User).filter_by(user_id=user_id)
                )
                user = user_res.scalar_one_or_none()
                if not user:
                    user = User(
                        user_id=user_id, 
                        exp=0, 
                        level=1, 
                        token_balance=0,
                        current_streak=0,
                        max_streak=0
                    )
                    session.add(user)
                    await session.flush() # Lấy dữ liệu tạm thời trước khi add Task

                # Kiểm tra trùng lịch với các mục tiêu đã có của người tạo
                existing_tasks_res = await session.execute(
                    select(Task).filter_by(user_id=user_id)
                )
                existing_tasks = existing_tasks_res.scalars().all()
                
                overlapping_task = None
                for t in existing_tasks:
                    if tasks_overlap(
                        parsed_time, duration_minutes, parsed_date,
                        t.start_time, t.duration_minutes, t.start_date
                    ):
                        overlapping_task = t
                        break
                
                if overlapping_task:
                    date_str = overlapping_task.start_date.strftime("%d/%m/%Y") if overlapping_task.start_date else ""
                    dummy_dt = datetime.datetime.combine(datetime.date.today(), overlapping_task.start_time)
                    end_dt = dummy_dt + datetime.timedelta(minutes=overlapping_task.duration_minutes)
                    end_str = end_dt.strftime("%H:%M")
                    end_suffix = " (ngày hôm sau)" if end_dt.date() > dummy_dt.date() else ""
                    time_range_str = f"{date_str + ' ' if date_str else ''}{overlapping_task.start_time.strftime('%H:%M')} - {end_str}{end_suffix}"
                    
                    await interaction.followup.send(
                        f"❌ **Không thể tạo mục tiêu!** Phiên làm việc không được tạo vì chồng thời gian với phiên làm việc đã đăng ký trước đó:\n"
                        f"• Mục tiêu trùng: `{overlapping_task.title}` (`{time_range_str}`)\n"
                        f"Vui lòng chọn khung giờ khác.",
                        ephemeral=True
                    )
                    return

                # Tạo mới Task cho User
                new_task = Task(
                    user_id=user_id,
                    title=self.task_name.value,
                    start_time=parsed_time,
                    duration_minutes=duration_minutes,
                    start_date=parsed_date,
                    task_type=self.task_type
                )
                session.add(new_task)
                await session.flush() # Lấy new_task.task_id

                invited_mentions = []
                failed_mentions = []

                # Tạo lời mời và gửi DM
                for colab in collaborators:
                    if colab.id == user_id:
                        continue # Bỏ qua nếu tự mời chính mình

                    invite = TaskInvite(
                        task_id=new_task.task_id,
                        invitee_id=colab.id,
                        status="pending"
                    )
                    session.add(invite)
                    await session.flush()

                    try:
                        embed = discord.Embed(
                            title="🤝 LỜI MỜI LÀM VIỆC CÙNG 🤝",
                            description=(
                                f"**{interaction.user.display_name}** đã mời bạn cùng thực hiện mục tiêu tập trung:\n\n"
                                f"• **Mục tiêu:** `{self.task_name.value}`\n"
                                f"• **Loại mục tiêu:** `{self.task_type}`\n"
                                f"• **Ngày bắt đầu:** `{self.start_date.value}`\n"
                                f"• **Thời gian bắt đầu:** `{self.start_time.value}`\n"
                                f"• **Thời lượng:** `{duration_minutes} phút`\n"
                                f"• **Cách tham gia:** Khi đến giờ hãy vào phòng {self.task_type}.\n\n"
                                f"Bạn có đồng ý tham gia mục tiêu này cùng **{interaction.user.display_name}** không?\n"
                                f"*(Nếu đồng ý, mục tiêu này sẽ được áp dụng cho bạn, bao gồm các thông báo, tính điểm và phần thưởng)*"
                            ),
                            color=discord.Color.blue()
                        )
                        # Gửi DM kèm view nút bấm
                        msg = await colab.send(embed=embed, view=TaskInviteView())
                        invite.message_id = msg.id
                        invited_mentions.append(colab.mention)
                    except discord.Forbidden:
                        logger.warning(f"Không thể gửi DM mời cho {colab.name} (Chặn DM).")
                        await session.delete(invite)
                        failed_mentions.append(colab.name)
                    except Exception as e:
                        logger.error(f"Lỗi khi gửi DM mời cho {colab.name}: {e}")
                        await session.delete(invite)
                        failed_mentions.append(colab.name)

                await session.commit()
                
            logger.info(f"User {user_id} đăng ký task '{self.task_name.value}' lúc {self.start_time.value} thành công.")
            
            # Phản hồi lại cho user thành công dưới dạng ẩn (ephemeral)
            response_msg = (
                f"🎯 **Đăng ký mục tiêu thành công!**\n"
                f"• **Mục tiêu:** `{self.task_name.value}`\n"
                f"• **Loại mục tiêu:** `{self.task_type}`\n"
                f"• **Ngày bắt đầu:** `{self.start_date.value}`\n"
                f"• **Thời gian bắt đầu:** `{self.start_time.value}`\n"
                f"• **Thời lượng:** `{duration_minutes} phút`\n"
            )
            if invited_mentions:
                response_msg += f"• **Đã gửi lời mời làm việc cùng tới:** {', '.join(invited_mentions)}\n"
            if failed_mentions:
                response_msg += f"• **Không thể gửi lời mời tới (do chặn DM):** {', '.join(failed_mentions)}\n"
            
            response_msg += f"\n🔔 *Bot sẽ tự động nhắc nhở trước giờ học/làm việc 5 phút.*"
            response_msg += f"\n🔊 *Khi đến giờ hãy vào phòng {self.task_type}.*"

            await interaction.followup.send(response_msg, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Lỗi khi lưu task cho user {user_id}: {e}")
            await interaction.followup.send(
                "❌ **Lỗi hệ thống!** Không thể lưu mục tiêu của bạn lúc này. Vui lòng liên hệ Admin.",
                ephemeral=True
            )



class TaskTypeSelectionView(discord.ui.View):
    """
    View chứa các nút lựa chọn Loại mục tiêu trước khi mở Modal đăng ký.
    """
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(
        label="🎮 Giải trí",
        style=discord.ButtonStyle.blurple,
        custom_id="chronos_btn_select_giai_tri"
    )
    async def select_giai_tri(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TaskRegistrationModal(task_type="giải trí"))

    @discord.ui.button(
        label="💼 Làm việc",
        style=discord.ButtonStyle.green,
        custom_id="chronos_btn_select_lam_viec"
    )
    async def select_lam_viec(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TaskRegistrationModal(task_type="làm việc"))

    @discord.ui.button(
        label="📚 Học tập",
        style=discord.ButtonStyle.blurple,
        custom_id="chronos_btn_select_hoc_tap"
    )
    async def select_hoc_tap(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TaskRegistrationModal(task_type="học tập"))


class ControlPanelView(discord.ui.View):
    """
    View chứa các nút bấm tĩnh trên Control Panel.
    Sử dụng timeout=None và custom_id cố định trên các nút để hoạt động như Persistent View
    (View tồn tại vĩnh viễn qua các lần restart bot mà không bị mất callback tương tác).
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Đăng ký mục tiêu 🎯",
        style=discord.ButtonStyle.green,
        custom_id="chronos_btn_register_task"
    )
    async def register_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Defer trước để tránh lỗi Unknown interaction (10062) nếu bot phản hồi trễ hoặc lag mạng
        await interaction.response.defer(ephemeral=True)
        # Hiển thị lựa chọn loại mục tiêu qua followup
        await interaction.followup.send(
            content="Vui lòng chọn loại mục tiêu tập trung của bạn:",
            view=TaskTypeSelectionView(),
            ephemeral=True
        )

    @discord.ui.button(
        label="Mục tiêu của tôi 📅",
        style=discord.ButtonStyle.blurple,
        custom_id="chronos_btn_my_tasks"
    )
    async def my_tasks(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        
        try:
            async with get_db_session() as session:
                tasks = await get_active_and_upcoming_tasks(session, user_id)
                
            if not tasks:
                await interaction.followup.send("📭 Bạn không có mục tiêu hiện tại hoặc sắp tới nào.", ephemeral=True)
                return
                
            embed = discord.Embed(
                title="📅 MỤC TIÊU ĐÃ ĐĂNG KÝ CỦA BẠN",
                description="Dưới đây là danh sách các mục tiêu tập trung hằng ngày của bạn:",
                color=discord.Color.purple()
            )
            
            day_names = {
                0: "T2",
                1: "T3",
                2: "T4",
                3: "T5",
                4: "T6",
                5: "T7",
                6: "CN"
            }
            
            for i, task in enumerate(tasks, 1):
                start_time_str = task.start_time.strftime("%H:%M")
                start_date_str = task.start_date.strftime("%d/%m/%Y") if task.start_date else "Chưa rõ"
                
                type_emoji = "🎮" if task.task_type == "giải trí" else "💼" if task.task_type == "làm việc" else "📚"
                embed.add_field(
                    name=f"{i}. {type_emoji} {task.title}",
                    value=(
                        f"• **Loại mục tiêu:** `{task.task_type}`\n"
                        f"• **Ngày bắt đầu:** `{start_date_str}`\n"
                        f"• **Thời gian:** `{start_time_str}` (`{task.duration_minutes} phút`)"
                    ),
                    inline=False
                )
                
            embed.set_footer(text=f"Tổng số: {len(tasks)} mục tiêu")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Lỗi khi hiển thị danh sách mục tiêu của user {user_id}: {e}")
            await interaction.followup.send("❌ Đã xảy ra lỗi hệ thống khi tải danh sách mục tiêu của bạn.", ephemeral=True)

    @discord.ui.button(
        label="Túi đồ & Cửa hàng 🎒",
        style=discord.ButtonStyle.blurple,
        custom_id="chronos_btn_inventory_shop"
    )
    async def inventory_shop(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Gọi sang Cog Economy để mở menu tương tác túi đồ/cửa hàng
        economy_cog = interaction.client.get_cog("Economy")
        if economy_cog:
            await economy_cog.show_inventory_shop(interaction)
        else:
            await interaction.response.send_message("❌ Hệ thống Kinh tế đang được bảo trì. Vui lòng liên hệ Admin.", ephemeral=True)



class ControlPanel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Đăng ký View vào Bot để bắt các sự kiện từ các nút cũ đã gửi trên Server Discord
        self.bot.add_view(ControlPanelView())
        self.bot.add_view(TaskInviteView())
        # Bắt đầu tác vụ dọn dẹp định kỳ mỗi 5 phút
        self.clean_panel_channel.start()

    def cog_unload(self):
        # Hủy tác vụ dọn dẹp khi Cog bị unload
        self.clean_panel_channel.cancel()

    def get_panel_embed(self) -> discord.Embed:
        """
        Tạo và trả về Embed cho Bảng điều khiển chính.
        """
        embed = discord.Embed(
            title="🎛️ TRUNG TÂM ĐIỀU KHIỂN CHRONOS 🎛️",
            description=(
                "Chào mừng bạn đến với **Hệ thống Kỷ luật và Quản lý Thời gian Chronos**!\n\n"
                "Sử dụng các nút bấm dưới đây để thiết lập lịch trình học tập hoặc mua sắm vật phẩm:\n\n"
                "🎯 **Đăng ký mục tiêu:** Tạo mục tiêu tập trung cá nhân hằng ngày.\n"
                "📅 **Mục tiêu của tôi:** Xem danh sách các mục tiêu tập trung đã đăng ký.\n"
                "🎒 **Túi đồ & Cửa hàng:** Xem các trang phục, thẻ nghỉ phép, thẻ xóa vi phạm.\n\n"
                "--- \n"
                "💡 *Mọi thao tác bấm nút đều trả về phản hồi ẩn (chỉ bạn mới thấy), tránh gây loãng kênh.*"
            ),
            color=discord.Color.purple()
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url if self.bot.user else None)
        embed.set_footer(text="Chronos Bot • Ép buộc kỷ luật, mở khóa tiềm năng! 💪")
        return embed

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("ControlPanel Cog đã sẵn sàng.")

    @tasks.loop(minutes=5)
    async def clean_panel_channel(self):
        """
        Tác vụ dọn dẹp kênh định kỳ mỗi 5 phút.
        """
        logger.info("Đang chạy tác vụ kiểm tra định kỳ cho kênh #control-panel...")
        await self.initialize_control_panels()

    @clean_panel_channel.before_loop
    async def before_clean_panel(self):
        # Đợi bot sẵn sàng trước khi chạy tác vụ lặp
        await self.bot.wait_until_ready()

    async def initialize_control_panels(self):
        """
        Tự động tìm kiếm kênh 'control-panel' trên toàn bộ các server bot tham gia,
        xóa sạch tin nhắn cũ và gửi bảng điều khiển cố định nếu chưa có.
        """
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name="control-panel")
            if not channel:
                logger.warning(f"Không tìm thấy kênh #control-panel tại server '{guild.name}'.")
                continue

            # Kiểm tra quyền hạn của bot
            permissions = channel.permissions_for(guild.me)
            if not (permissions.read_messages and permissions.send_messages and permissions.manage_messages and permissions.read_message_history):
                logger.error(
                    f"Bot thiếu quyền hạn trong kênh #control-panel tại server '{guild.name}'. "
                    f"Vui lòng cấp các quyền: Đọc tin nhắn, Gửi tin nhắn, Quản lý tin nhắn, Đọc lịch sử tin nhắn."
                )
                continue

            try:
                # Đọc các tin nhắn gần nhất
                messages = []
                async for message in channel.history(limit=5):
                    messages.append(message)

                embed = self.get_panel_embed()
                should_recreate = True

                # Nếu kênh chỉ có duy nhất 1 tin nhắn và đó chính là bảng điều khiển của bot
                if len(messages) == 1:
                    msg = messages[0]
                    if msg.author.id == self.bot.user.id and msg.embeds:
                        if msg.embeds[0].title == embed.title:
                            should_recreate = False
                            logger.info(f"Bảng điều khiển đã sạch sẽ và sẵn sàng tại server '{guild.name}'.")

                if should_recreate:
                    logger.info(f"Đang dọn dẹp và tạo mới Bảng điều khiển tại kênh #control-panel server '{guild.name}'...")
                    # Xóa tối đa 100 tin nhắn cũ
                    await channel.purge(limit=100)
                    # Gửi bảng điều khiển mới kèm theo view tương tác
                    await channel.send(embed=embed, view=ControlPanelView())
                    logger.info(f"Đã cập nhật Bảng điều khiển mới tại server '{guild.name}'.")

            except Exception as e:
                logger.error(f"Lỗi khi thiết lập kênh control-panel tại server '{guild.name}': {e}")

    @commands.hybrid_command(name="panel", description="Khởi tạo hoặc dọn dẹp và hiển thị lại Bảng điều khiển")
    async def panel(self, ctx: commands.Context):
        """
        Khởi tạo hoặc dọn dẹp và hiển thị lại Bảng điều khiển chính trực tiếp tại kênh.
        """
        # Cấu hình kiểm tra kênh (chỉ cho phép chạy ở kênh control-panel)
        if ctx.channel.name != "control-panel":
            if ctx.interaction:
                await ctx.send("❌ Lệnh này chỉ có thể sử dụng tại kênh `control-panel`!", ephemeral=True)
            else:
                try:
                    await ctx.message.delete()
                except discord.HTTPException:
                    pass
            return

        # Defer phản hồi nếu là slash command để tránh timeout
        if ctx.interaction:
            await ctx.interaction.response.defer(ephemeral=True)

        try:
            # Nếu là prefix command, cố gắng xóa tin nhắn lệnh gốc
            if not ctx.interaction:
                try:
                    await ctx.message.delete()
                except discord.HTTPException:
                    pass

            # Dọn dẹp và khởi tạo lại panel trực tiếp tại kênh
            logger.info(f"Yêu cầu làm mới panel thủ công từ {ctx.author.name} tại kênh #control-panel.")
            await ctx.channel.purge(limit=100)
            embed = self.get_panel_embed()
            await ctx.channel.send(embed=embed, view=ControlPanelView())

            if ctx.interaction:
                await ctx.interaction.followup.send("✅ Đã dọn dẹp kênh và tạo lại Bảng điều khiển thành công!", ephemeral=True)

        except Exception as e:
            logger.error(f"Lỗi khi dọn dẹp và gửi lại panel: {e}")
            if ctx.interaction:
                await ctx.interaction.followup.send(f"❌ Đã xảy ra lỗi khi tạo panel: {e}", ephemeral=True)

    @panel.error
    async def panel_error(self, ctx: commands.Context, error: commands.CommandError):
        """
        Xử lý lỗi phát sinh khi chạy lệnh panel.
        """
        logger.error(f"Lỗi khi thực hiện lệnh panel: {error}")
        if ctx.interaction:
            await ctx.send(f"❌ Đã xảy ra lỗi: {error}", ephemeral=True)
        else:
            await ctx.send(f"❌ Đã xảy ra lỗi: {error}", delete_after=10)

async def setup(bot: commands.Bot):
    await bot.add_cog(ControlPanel(bot))
