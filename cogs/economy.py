import logging
import random
import asyncio
import datetime
import os
import discord
from discord.ext import commands, tasks
from sqlalchemy import select, update, delete
from database.db_session import get_db_session
from database.models import User, Item, Inventory, ViolationLog, FocusSession
import config

logger = logging.getLogger("ChronosBot.Economy")

# ----------------- HELPER FUNCTIONS CHO ROLE MÀU SẮC -----------------

async def remove_user_color_roles(member: discord.Member):
    """
    Xóa tất cả các role màu tên do Chronos gán để chuẩn bị đổi màu mới.
    """
    roles_to_remove = []
    for role in member.roles:
        if role.name.startswith("Chronos Color:") or role.name == "Chronos Chameleon":
            roles_to_remove.append(role)
    if roles_to_remove:
        try:
            await member.remove_roles(*roles_to_remove, reason="Gỡ bỏ màu tên cũ")
        except Exception as e:
            logger.error(f"Lỗi khi gỡ role màu tên cho {member.name}: {e}")

async def ensure_color_role(guild: discord.Guild, color_name: str) -> discord.Role:
    """
    Tạo hoặc lấy role màu sắc tương ứng, di chuyển vị trí hiển thị lên cao để có tác dụng.
    """
    role_name = f"Chronos Color: {color_name}" if color_name != "Chameleon" else "Chronos Chameleon"
    role = discord.utils.get(guild.roles, name=role_name)
    if not role:
        color_hex = {
            "Neon Pink": discord.Color.from_rgb(255, 20, 147),
            "Hacker Green": discord.Color.from_rgb(57, 255, 20),
            "Blood Red": discord.Color.from_rgb(255, 0, 0),
            "Chameleon": discord.Color.purple()
        }.get(color_name, discord.Color.default())
        
        try:
            role = await guild.create_role(name=role_name, color=color_hex, reason="Tạo role màu tên từ Shop")
            # Đưa role lên vị trí cao để đè màu tên mặc định
            bot_member = guild.me
            bot_top_role = bot_member.top_role
            if bot_top_role.position > 1:
                try:
                    await role.edit(position=bot_top_role.position - 1)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Lỗi khi tạo role màu tên {role_name} trên {guild.name}: {e}")
    return role


import re

async def update_member_nickname_with_title(member: discord.Member, title: str = None):
    """
    Cập nhật biệt danh hiển thị kèm theo danh hiệu của thành viên dạng suffix cực chất.
    Ví dụ: faemi -> faemi | 💤 Kẻ thù giường ngủ
    """
    current_nick = member.nick or member.name
    
    base_name = current_nick
    # Match prefix [Danh hiệu] cũ để làm sạch nếu còn sót
    match_prefix = re.match(r"^\[.*?\]\s*(.*)$", current_nick)
    if match_prefix:
        base_name = match_prefix.group(1).strip()
    else:
        # Match suffix cũ: Name | ...
        match_suffix = re.match(r"^(.*?)\s*\|\s*.*$", current_nick)
        if match_suffix:
            base_name = match_suffix.group(1).strip()

    if title:
        # Mapping emoji tương ứng cho từng danh hiệu để hiển thị trực quan và thu hút hơn
        title_emojis = {
            "Đẹp trai có gì sai": "✨",
            "Hôm nay tôi buồn": "💧",
            "Chúa tể chạy Deadline": "⏰",
            "Kẻ thù của giường ngủ": "💤",
            "Kẻ Lập Dị": "🧩",
            "Thần Đồng": "🧠",
            "Khắc Kỷ Sư": "🧘",
            "Chiến Binh Kỷ Luật": "🛡️"
        }
        emoji = title_emojis.get(title, "👑") # Default là vương miện cho các title custom khác
        title_part = f" | {emoji} {title}"
        
        # Nếu vượt quá giới hạn 32 ký tự của Discord
        if len(base_name) + len(title_part) > 32:
            # Ưu tiên giữ nguyên tên hiển thị (base_name) và cắt bớt danh hiệu
            allowed_title_len = 32 - len(base_name) - len(f" | {emoji} ")
            if allowed_title_len > 3:
                truncated_title = title[:allowed_title_len-3] + "..."
                new_nick = f"{base_name} | {emoji} {truncated_title}"
            else:
                # Nếu tên quá dài, rút gọn tên xuống 15 ký tự và chừa chỗ cho danh hiệu
                short_base = base_name[:15] + ".."
                allowed_title_len = 32 - len(short_base) - len(f" | {emoji} ")
                if allowed_title_len > 3:
                    truncated_title = title[:allowed_title_len-3] + "..."
                    new_nick = f"{short_base} | {emoji} {truncated_title}"
                else:
                    new_nick = f"{short_base} | {emoji} {title[:10]}"
        else:
            new_nick = f"{base_name}{title_part}"
    else:
        new_nick = base_name

    # Tránh lặp vô hạn và API call thừa
    if new_nick == member.nick:
        return
    if member.nick is None and new_nick == member.name:
        return

    try:
        await member.edit(nick=new_nick, reason=f"Cập nhật danh hiệu: {title}")
        logger.info(f"Đã cập nhật biệt danh của {member.name} thành: {new_nick}")
    except discord.Forbidden:
        logger.warning(f"Không có quyền đổi nickname cho {member.name} (ID: {member.id})")
    except Exception as e:
        logger.error(f"Lỗi khi đổi nickname cho {member.name}: {e}")


# ----------------- MODALS CHO VẬT PHẨM ĐẶC BIỆT -----------------

class CustomTitleModal(discord.ui.Modal, title="Đặt Danh Hiệu Tự Chọn"):
    """
    Hộp thoại Modal để gõ danh hiệu tự chọn 30 ngày.
    """
    title_input = discord.ui.TextInput(
        label="Nội dung danh hiệu mong muốn",
        placeholder="Ví dụ: Vợ anh A, CEO Tương lai, Lập trình viên...",
        required=True,
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        title_text = self.title_input.value
        now = datetime.datetime.now()
        expiry = now + datetime.timedelta(days=30)

        async with get_db_session() as session:
            # Kiểm tra thẻ trong túi đồ
            inv_res = await session.execute(
                select(Inventory).filter_by(user_id=user_id, item_id="title_custom")
            )
            inv = inv_res.scalar_one_or_none()
            if not inv or inv.quantity <= 0:
                await interaction.followup.send("❌ Bạn không có **Gói Thẻ Tùy Biến** trong túi đồ.", ephemeral=True)
                return

            # Khấu trừ 1 thẻ
            if inv.quantity > 1:
                inv.quantity -= 1
            else:
                await session.delete(inv)

            # Cập nhật danh hiệu
            user_res = await session.execute(select(User).filter_by(user_id=user_id))
            user = user_res.scalar_one()
            user.custom_title = title_text
            user.active_title = title_text
            user.custom_title_expiry = expiry

            await session.commit()

        # Cập nhật nickname trên server
        if isinstance(interaction.user, discord.Member):
            await update_member_nickname_with_title(interaction.user, title_text)

        await interaction.followup.send(
            f"🎉 **Đeo danh hiệu tự chọn thành công!**\n"
            f"• Danh hiệu mới: `{title_text}`\n"
            f"• Hạn duy trì: 30 ngày (đến ngày `{expiry.strftime('%d/%m/%Y')}`).",
            ephemeral=True
        )


class LoudspeakerModal(discord.ui.Modal, title="Phát tin nhắn Loa Phường"):
    """
    Hộp thoại để nhập tin nhắn muốn ghim lên kênh Chat Chung.
    """
    message_input = discord.ui.TextInput(
        label="Nội dung thông điệp muốn phát",
        placeholder="Nhập nội dung ghim lên kênh Chat Chung...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        msg_text = self.message_input.value
        
        async with get_db_session() as session:
            inv_res = await session.execute(
                select(Inventory).filter_by(user_id=user_id, item_id="loudspeaker")
            )
            inv = inv_res.scalar_one_or_none()
            if not inv or inv.quantity <= 0:
                await interaction.followup.send("❌ Bạn không có **Loa Phường** trong túi đồ.", ephemeral=True)
                return
            
            if inv.quantity > 1:
                inv.quantity -= 1
            else:
                await session.delete(inv)
            await session.commit()

        # Phát loa phường lên kênh được chỉ định
        guild = interaction.guild
        channel = guild.get_channel(config.KENH_THONG_BAO_ID) if config.KENH_THONG_BAO_ID else None
        if not channel:
            channel = (
                discord.utils.get(guild.text_channels, name="chat-chung") or 
                discord.utils.get(guild.text_channels, name="general") or 
                interaction.channel
            )
            
        embed = discord.Embed(
            title="📢 BẢN TIN LOA PHƯỜNG 📢",
            description=f"## ❝ **{msg_text}** ❞\n\n*(Phát thanh từ {interaction.user.mention} — Ghim nổi bật trong 1 giờ)*",
            color=discord.Color.red(),
            timestamp=datetime.datetime.now()
        )
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        
        try:
            sent_msg = await channel.send(embed=embed)
            await sent_msg.pin()
            
            # Unpin sau 1 giờ
            async def unpin_later(message):
                await asyncio.sleep(3600)
                try:
                    await message.unpin()
                except Exception:
                    pass
            asyncio.create_task(unpin_later(sent_msg))
            
            await interaction.followup.send(f"📢 Đã phát và ghim tin nhắn Loa Phường tại kênh {channel.mention}!", ephemeral=True)
        except Exception as e:
            logger.error(f"Lỗi khi phát loa phường: {e}")
            await interaction.followup.send("❌ Bot thiếu quyền gửi hoặc ghim tin nhắn ở kênh Chat Chung.", ephemeral=True)


# ----------------- VIEWS CHỌN BẠN BÈ ĐỂ TƯƠNG TÁC -----------------

class FriendSelectionView(discord.ui.View):
    """
    View chứa dropdown UserSelect để chọn đối tượng bị Triệu Hồi hoặc Khóa Mõm.
    """
    def __init__(self, item_id: str):
        super().__init__(timeout=60)
        self.item_id = item_id
        
        user_select = discord.ui.UserSelect(placeholder="Chọn một người bạn trên server...", min_values=1, max_values=1)
        user_select.callback = self.user_selected_callback
        self.add_item(user_select)

    async def user_selected_callback(self, interaction: discord.Interaction):
        target_member = self.children[0].values[0]
        economy_cog = interaction.client.get_cog("Economy")
        if economy_cog:
            await economy_cog.execute_interactive_item_use(interaction, self.item_id, target_member)


# ----------------- SELECT MENUS CHO SHOP & INVENTORY -----------------

class EconomyDropdown(discord.ui.Select):
    """
    Dropdown chính: Xem túi đồ, Đại siêu thị, Tiệm cầm đồ.
    """
    def __init__(self):
        options = [
            discord.SelectOption(label="🎒 Túi đồ & Sử dụng", value="view_inventory", description="Xem túi đồ cá nhân, số dư và trang bị/sử dụng vật phẩm."),
            discord.SelectOption(label="🏪 Đại Siêu Thị (Grand Mall)", value="view_shop_categories", description="Vào shop đa phân khu: Cosmetics, Utility, Social."),
            discord.SelectOption(label="🤝 Tiệm cầm đồ (Pawn Shop)", value="view_pawn", description="Thanh lý lại các danh hiệu đã sở hữu lấy 50% Token.")
        ]
        super().__init__(placeholder="Chọn một hành động bạn muốn thực hiện...", min_values=1, max_values=1, options=options, custom_id="economy_main_dropdown")

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        economy_cog = interaction.client.get_cog("Economy")
        if not economy_cog:
            await interaction.response.send_message("❌ Hệ thống Kinh tế đang bảo trì.", ephemeral=True)
            return

        if value == "view_inventory":
            await economy_cog.handle_view_inventory(interaction)
        elif value == "view_shop_categories":
            await economy_cog.handle_view_shop_categories(interaction)
        elif value == "view_pawn":
            await economy_cog.handle_view_pawn(interaction)


class ShopCategorySelect(discord.ui.Select):
    """
    Dropdown chọn phân khu trong Grand Mall.
    """
    def __init__(self):
        options = [
            discord.SelectOption(label="🎭 Phân khu 1: Cosmetics (Danh hiệu & Màu tên)", value="cosmetic", description="Cá nhân hóa profile & màu hiển thị trên server."),
            discord.SelectOption(label="🛠️ Phân khu 2: Utility (Năng suất & Sinh tồn)", value="utility_survival", description="Cà phê, Thẻ nghỉ phép, Nhân đôi EXP, Xóa vi phạm."),
            discord.SelectOption(label="🎪 Phân khu 3: Social Interactions (Tương tác)", value="social", description="Loa phường, Nhạc Voice, Triệu hồi, Khóa mõm troll bạn bè.")
        ]
        super().__init__(placeholder="Chọn phân khu bạn muốn ghé thăm...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]
        economy_cog = interaction.client.get_cog("Economy")
        if economy_cog:
            await economy_cog.handle_view_shop_category_items(interaction, category)


class ShopItemSelect(discord.ui.Select):
    """
    Dropdown chọn hàng hóa mua.
    """
    def __init__(self, items_list):
        options = []
        for item in items_list:
            options.append(
                discord.SelectOption(
                    label=f"{item.name} ({item.price} Tokens)",
                    value=item.item_id,
                    description=item.description[:100] if item.description else "Không có mô tả."
                )
            )
        super().__init__(placeholder="Chọn vật phẩm bạn muốn mua...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        item_id = self.values[0]
        economy_cog = interaction.client.get_cog("Economy")
        if economy_cog:
            await economy_cog.handle_shop_item_details(interaction, item_id)


class PawnItemSelect(discord.ui.Select):
    """
    Dropdown chọn danh hiệu/màu sắc cầm đồ.
    """
    def __init__(self, inventory_items):
        options = []
        for inv, item in inventory_items:
            # Giá cầm bằng 50% giá gốc, riêng các danh hiệu hiếm giá 50 tokens -> bán nhận 25 tokens
            base_price = item.price if item.price > 0 else 50
            sell_price = int(base_price * 0.5)
            options.append(
                discord.SelectOption(
                    label=f"{item.name} (Bán nhận: {sell_price} Tokens) x{inv.quantity}",
                    value=item.item_id,
                    description=f"Giá trị quy đổi: {base_price} Tokens. Click để xác nhận thanh lý."
                )
            )
        super().__init__(placeholder="Chọn vật phẩm trang trí bạn muốn bán lại...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        item_id = self.values[0]
        economy_cog = interaction.client.get_cog("Economy")
        if economy_cog:
            await economy_cog.handle_pawn_sell_confirm(interaction, item_id)


# Dropdown sử dụng vật phẩm từ túi đồ
class InventoryUseSelect(discord.ui.Select):
    def __init__(self, usable_items):
        options = []
        for inv, item in usable_items:
            options.append(
                discord.SelectOption(
                    label=f"{item.name} (Số lượng: x{inv.quantity})",
                    value=item.item_id,
                    description=item.description[:100]
                )
            )
        super().__init__(placeholder="Chọn vật phẩm bạn muốn sử dụng...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        item_id = self.values[0]
        economy_cog = interaction.client.get_cog("Economy")
        if economy_cog:
            await economy_cog.handle_inventory_item_use(interaction, item_id)


# Dropdown trang bị cosmetics từ túi đồ
class InventoryEquipSelect(discord.ui.Select):
    def __init__(self, equip_options):
        # equip_options: list của tuple (value, label, description)
        options = []
        for val, name, desc in equip_options:
            options.append(
                discord.SelectOption(
                    label=name,
                    value=val,
                    description=desc[:100]
                )
            )
        super().__init__(placeholder="Chọn danh hiệu hoặc màu sắc để trang bị...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        choice_val = self.values[0]
        economy_cog = interaction.client.get_cog("Economy")
        if economy_cog:
            await economy_cog.execute_equip_item(interaction, choice_val)


# ----------------- VIEWS CHO SHOP & INVENTORY -----------------

class MainEconomyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(EconomyDropdown())


class BackToMainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="⬅️ Quay lại Menu chính", style=discord.ButtonStyle.grey)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🎒 HỆ THỐNG ĐẠI SIÊU THỊ & TÚI ĐỒ 🏪",
            description=(
                "Hãy chọn một hành động trong Dropdown Menu phía dưới:\n\n"
                "• **Túi đồ cá nhân:** Xem số dư và trang bị/sử dụng vật phẩm sở hữu.\n"
                "• **Vào Đại Siêu Thị (Grand Mall):** Mua các vật phẩm và danh hiệu.\n"
                "• **Tiệm cầm đồ (Pawn Shop):** Bán lại trang phục lấy Token khẩn cấp."
            ),
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(embed=embed, view=MainEconomyView())


class BuyQuantityModal(discord.ui.Modal):
    def __init__(self, parent_view: 'PurchaseConfirmationView', item_name: str):
        super().__init__(title="Nhập số lượng muốn mua")
        self.parent_view = parent_view
        
        self.quantity_input = discord.ui.TextInput(
            label=f"Số lượng mua {item_name[:20]}...",
            placeholder="Nhập số lượng (Ví dụ: 1, 5, 10)...",
            default=str(parent_view.quantity),
            min_length=1,
            max_length=5,
            required=True
        )
        self.add_item(self.quantity_input)

    async def on_submit(self, interaction: discord.Interaction):
        qty_str = self.quantity_input.value.strip()
        if not qty_str.isdigit():
            await interaction.response.send_message("❌ Vui lòng nhập một số nguyên dương hợp lệ.", ephemeral=True)
            return
        
        qty = int(qty_str)
        if qty <= 0:
            await interaction.response.send_message("❌ Số lượng muốn mua phải lớn hơn 0.", ephemeral=True)
            return

        self.parent_view.quantity = qty
        self.parent_view.update_button_label()
        
        economy_cog = interaction.client.get_cog("Economy")
        if economy_cog:
            await economy_cog.update_purchase_details_message(interaction, self.parent_view)


class PurchaseConfirmationView(discord.ui.View):
    def __init__(self, item_id: str, price: int, quantity: int = 1):
        super().__init__(timeout=60)
        self.item_id = item_id
        self.price = price
        self.quantity = quantity
        self.update_button_label()

    def update_button_label(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label.startswith("🟢 Xác nhận Mua"):
                child.label = f"🟢 Xác nhận Mua (x{self.quantity})"

    @discord.ui.button(label="🟢 Xác nhận Mua (x1)", style=discord.ButtonStyle.green)
    async def confirm_buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        economy_cog = interaction.client.get_cog("Economy")
        if economy_cog:
            await economy_cog.execute_purchase(interaction, self.item_id, self.price, self.quantity)

    @discord.ui.button(label="🔢 Nhập số lượng", style=discord.ButtonStyle.blurple)
    async def input_quantity(self, interaction: discord.Interaction, button: discord.ui.Button):
        economy_cog = interaction.client.get_cog("Economy")
        if economy_cog:
            async with get_db_session() as session:
                item_res = await session.execute(select(Item).filter_by(item_id=self.item_id))
                item = item_res.scalar_one_or_none()
                item_name = item.name if item else self.item_id
            
            await interaction.response.send_modal(BuyQuantityModal(self, item_name))

    @discord.ui.button(label="🔴 Hủy giao dịch", style=discord.ButtonStyle.red)
    async def cancel_buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Giao dịch đã bị hủy.", embed=None, view=BackToMainView())


class SellConfirmationView(discord.ui.View):
    def __init__(self, item_id: str, sell_price: int):
        super().__init__(timeout=60)
        self.item_id = item_id
        self.sell_price = sell_price

    @discord.ui.button(label="🤝 Xác nhận Bán lại", style=discord.ButtonStyle.danger)
    async def confirm_sell(self, interaction: discord.Interaction, button: discord.ui.Button):
        economy_cog = interaction.client.get_cog("Economy")
        if economy_cog:
            await economy_cog.execute_sell(interaction, self.item_id, self.sell_price)

    @discord.ui.button(label="🔴 Hủy bỏ", style=discord.ButtonStyle.grey)
    async def cancel_sell(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Giao dịch cầm đồ đã bị hủy.", embed=None, view=BackToMainView())


# ----------------- COG ECONOMY CHÍNH -----------------

class Economy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Khởi động loop đổi màu Chameleon & Quét hạn dùng Custom Title
        self.chameleon_loop.start()
        self.check_custom_title_expiries.start()

    def cog_unload(self):
        self.chameleon_loop.cancel()
        self.check_custom_title_expiries.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("Economy Cog đã sẵn sàng.")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.bot:
            return
        if before.nick == after.nick:
            return

        async with get_db_session() as session:
            user_res = await session.execute(select(User).filter_by(user_id=after.id))
            user = user_res.scalar_one_or_none()

        active_title = user.active_title if user else None
        await update_member_nickname_with_title(after, active_title)

    # Loop đổi màu chameleon hàng giờ
    @tasks.loop(hours=1)
    async def chameleon_loop(self):
        await self.bot.wait_until_ready()
        logger.info("Chạy loop Tắc Kè Hoa đổi màu...")
        for guild in self.bot.guilds:
            role = discord.utils.get(guild.roles, name="Chronos Chameleon")
            if role:
                try:
                    random_color = discord.Color(random.randint(0, 0xFFFFFF))
                    await role.edit(color=random_color, reason="Tắc Kè Hoa đổi màu tự động")
                    logger.info(f"Đã đổi màu role Chronos Chameleon thành {random_color} trên server {guild.name}")
                except Exception as e:
                    logger.error(f"Lỗi khi đổi màu Chameleon trên server {guild.name}: {e}")

    @chameleon_loop.before_loop
    async def before_chameleon(self):
        await self.bot.wait_until_ready()

    # Loop quét hết hạn danh hiệu custom hằng ngày
    @tasks.loop(hours=24)
    async def check_custom_title_expiries(self):
        await self.bot.wait_until_ready()
        logger.info("Quét kiểm tra thời hạn danh hiệu tự chọn...")
        now = datetime.datetime.now()
        expired_user_ids = []
        async with get_db_session() as session:
            res = await session.execute(
                select(User).where(User.custom_title_expiry.isnot(None), User.custom_title_expiry <= now)
            )
            expired_users = res.scalars().all()
            for user in expired_users:
                # Nếu đang đeo custom title này, gỡ bỏ
                if user.active_title == user.custom_title:
                    user.active_title = None
                user.custom_title = None
                user.custom_title_expiry = None
                expired_user_ids.append(user.user_id)
            
            if expired_users:
                await session.commit()
                
        # Gửi thông báo DM ngoài session block
        for user_id in expired_user_ids:
            try:
                member = None
                for guild in self.bot.guilds:
                    member = guild.get_member(user_id)
                    if member:
                        break
                if member:
                    await member.send("🎫 **Danh hiệu tự chọn của bạn đã hết hạn 30 ngày.** Hãy mua Gói Thẻ Tùy Biến mới tại Shop để thiết lập lại!")
            except Exception as e:
                logger.warning(f"Không thể gửi thông báo hết hạn danh hiệu cho {user_id}: {e}")

    @check_custom_title_expiries.before_loop
    async def before_check_expiries(self):
        await self.bot.wait_until_ready()

    async def show_inventory_shop(self, interaction: discord.Interaction):
        """
        Giao diện chính gọi từ Control Panel.
        """
        embed = discord.Embed(
            title="🎒 HỆ THỐNG ĐẠI SIÊU THỊ & TÚI ĐỒ 🏪",
            description=(
                "Hãy chọn một hành động trong Dropdown Menu phía dưới:\n\n"
                "• **Túi đồ cá nhân:** Xem số dư và trang bị/sử dụng vật phẩm sở hữu.\n"
                "• **Vào Đại Siêu Thị (Grand Mall):** Mua các vật phẩm và danh hiệu.\n"
                "• **Tiệm cầm đồ (Pawn Shop):** Bán lại trang phục lấy Token khẩn cấp."
            ),
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, view=MainEconomyView(), ephemeral=True)

    # ----------------- HIỂN THỊ TÚI ĐỒ -----------------
    async def handle_view_inventory(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        async with get_db_session() as session:
            user_res = await session.execute(select(User).filter_by(user_id=user_id))
            user = user_res.scalar_one_or_none()
            
            inv_res = await session.execute(
                select(Inventory, Item)
                .join(Item, Inventory.item_id == Item.item_id)
                .where(Inventory.user_id == user_id)
            )
            inventory_list = inv_res.all()

        tokens = user.token_balance if user else 0
        embed = discord.Embed(
            title=f"🎒 TÚI ĐỒ CỦA {interaction.user.display_name}",
            description=f"🪙 **Số dư tài khoản:** `{tokens} Tokens`",
            color=discord.Color.blue()
        )

        usable_items = []
        has_cosmetic = False

        if not inventory_list:
            embed.add_field(name="Vật phẩm sở hữu", value="*Túi đồ của bạn hiện đang trống!*", inline=False)
        else:
            items_str = ""
            for inv, item in inventory_list:
                items_str += f"• **{item.name}** (`{item.item_id}`) — Số lượng: **x{inv.quantity}**\n*{item.description}*\n\n"
                # Phân loại để hiển thị button tương tác
                if item.item_id in ["coffee", "eraser", "x2_speed", "loudspeaker", "summon_card", "muzzle_card"]:
                    usable_items.append((inv, item))
                if item.item_type == "cosmetic":
                    has_cosmetic = True
            
            # Nếu người dùng đã mở khóa student title pack
            if user and user.unlocked_student_titles:
                has_cosmetic = True
            if user and user.custom_title:
                has_cosmetic = True

            embed.add_field(name="Danh sách vật phẩm", value=items_str, inline=False)

        # Tạo view tương tác túi đồ
        view = discord.ui.View(timeout=120)
        
        # Nút sử dụng vật phẩm
        if usable_items:
            use_btn = discord.ui.Button(label="📦 Sử dụng Vật phẩm", style=discord.ButtonStyle.green, row=0)
            async def use_callback(inter: discord.Interaction):
                use_view = discord.ui.View()
                use_view.add_item(InventoryUseSelect(usable_items))
                back_to_inv = discord.ui.Button(label="⬅️ Quay lại Túi đồ", style=discord.ButtonStyle.grey, row=1)
                async def back_to_inv_callback(i: discord.Interaction):
                    await self.handle_view_inventory(i)
                back_to_inv.callback = back_to_inv_callback
                use_view.add_item(back_to_inv)
                await inter.response.edit_message(content="Chọn vật phẩm bạn muốn sử dụng dưới đây:", embed=None, view=use_view)
            use_btn.callback = use_callback
            view.add_item(use_btn)

        # Nút trang bị cosmetics
        if has_cosmetic:
            equip_btn = discord.ui.Button(label="🎭 Trang bị Danh hiệu/Màu sắc", style=discord.ButtonStyle.blurple, row=0)
            async def equip_callback(inter: discord.Interaction):
                # Tạo list các options trang bị dựa trên những gì người dùng mở khóa
                equip_options = []
                
                # Gỡ bỏ trang bị
                equip_options.append(("unequip_all", "❌ Tháo toàn bộ trang bị", "Gỡ toàn bộ danh hiệu và màu tên đang đeo"))

                # 1. Gói Title sinh viên
                if user and user.unlocked_student_titles:
                    equip_options.extend([
                        ("title_sv1", "🎭 Title: Đẹp trai có gì sai", "Danh hiệu gói Sinh viên"),
                        ("title_sv2", "🎭 Title: Hôm nay tôi buồn", "Danh hiệu gói Sinh viên"),
                        ("title_sv3", "🎭 Title: Chúa tể chạy Deadline", "Danh hiệu gói Sinh viên"),
                        ("title_sv4", "🎭 Title: Kẻ thù của giường ngủ", "Danh hiệu gói Sinh viên")
                    ])
                
                # 2. Danh hiệu tùy biến
                if user and user.custom_title:
                    equip_options.append(("title_custom_equip", f"🎭 Title: {user.custom_title} (Custom)", "Trang bị danh hiệu tự đặt của bạn"))

                # 3. Màu dạ quang
                has_glow = False
                for inv, item in inventory_list:
                    if item.item_id == "name_color":
                        has_glow = True
                if has_glow:
                    equip_options.extend([
                        ("color_neon", "🌈 Màu: Neon Pink", "Đổi màu tên thành Neon Pink"),
                        ("color_green", "🌈 Màu: Hacker Green", "Đổi màu tên thành Hacker Green"),
                        ("color_red", "🌈 Màu: Blood Red", "Đổi màu tên thành Blood Red")
                    ])

                # 4. Hiệu ứng Tắc kè hoa
                has_chameleon = False
                for inv, item in inventory_list:
                    if item.item_id == "chameleon":
                        has_chameleon = True
                if has_chameleon:
                    equip_options.append(("color_chameleon", "🦎 Màu: Tắc Kè Hoa", "Đổi màu tên tự động mỗi giờ"))



                equip_view = discord.ui.View()
                equip_view.add_item(InventoryEquipSelect(equip_options))
                
                # Nút đặt Custom Title nếu sở hữu thẻ tùy biến
                has_custom_card = False
                for inv, item in inventory_list:
                    if item.item_id == "title_custom":
                        has_custom_card = True
                if has_custom_card:
                    custom_setup_btn = discord.ui.Button(label="✍️ Đặt Custom Title mới", style=discord.ButtonStyle.green, row=1)
                    async def custom_setup_callback(i: discord.Interaction):
                        await i.response.send_modal(CustomTitleModal())
                    custom_setup_btn.callback = custom_setup_callback
                    equip_view.add_item(custom_setup_btn)

                back_to_inv = discord.ui.Button(label="⬅️ Quay lại Túi đồ", style=discord.ButtonStyle.grey, row=1)
                async def back_to_inv_callback(i: discord.Interaction):
                    await self.handle_view_inventory(i)
                back_to_inv.callback = back_to_inv_callback
                equip_view.add_item(back_to_inv)

                await inter.response.edit_message(content="Chọn trang bị bạn muốn đeo/tháo ở menu dưới:", embed=None, view=equip_view)
            
            equip_btn.callback = equip_callback
            view.add_item(equip_btn)

        # Nút back
        back_btn = discord.ui.Button(label="⬅️ Quay lại", style=discord.ButtonStyle.grey, row=0)
        async def back_to_main(inter: discord.Interaction):
            embed_main = discord.Embed(
                title="🎒 HỆ THỐNG ĐẠI SIÊU THỊ & TÚI ĐỒ 🏪",
                description=(
                    "Hãy chọn một hành động trong Dropdown Menu phía dưới:\n\n"
                    "• **Túi đồ cá nhân:** Xem số dư và trang bị/sử dụng vật phẩm sở hữu.\n"
                    "• **Vào Đại Siêu Thị (Grand Mall):** Mua các vật phẩm và danh hiệu.\n"
                    "• **Tiệm cầm đồ (Pawn Shop):** Bán lại trang phục lấy Token khẩn cấp."
                ),
                color=discord.Color.blue()
            )
            await inter.response.edit_message(embed=embed_main, view=MainEconomyView())
        back_btn.callback = back_to_main
        view.add_item(back_btn)

        await interaction.response.edit_message(embed=embed, view=view)


    # ----------------- XỬ LÝ TRANG BỊ CÁ NHÂN HÓA -----------------
    async def execute_equip_item(self, interaction: discord.Interaction, choice: str):
        """
        Xử lý logic khi người dùng bấm trang bị danh hiệu hoặc màu tên.
        """
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        guild = interaction.guild

        async with get_db_session() as session:
            user_res = await session.execute(select(User).filter_by(user_id=user_id))
            user = user_res.scalar_one()

            # Tháo toàn bộ trang bị
            if choice == "unequip_all":
                user.active_title = None
                user.active_color = None
                user.chameleon_enabled = False
                await session.commit()
                if guild:
                    await remove_user_color_roles(interaction.user)
                    if isinstance(interaction.user, discord.Member):
                        await update_member_nickname_with_title(interaction.user, None)
                await interaction.followup.send("✅ Đã tháo toàn bộ danh hiệu và màu tên của bạn.", ephemeral=True)
                return

            # Trang bị danh hiệu Sinh Viên
            sv_titles = {
                "title_sv1": "Đẹp trai có gì sai",
                "title_sv2": "Hôm nay tôi buồn",
                "title_sv3": "Chúa tể chạy Deadline",
                "title_sv4": "Kẻ thù của giường ngủ"
            }
            if choice in sv_titles:
                if not user.unlocked_student_titles:
                    await interaction.followup.send("❌ Bạn chưa mở khóa Gói Title Sinh Viên.", ephemeral=True)
                    return
                user.active_title = sv_titles[choice]
                await session.commit()
                if isinstance(interaction.user, discord.Member):
                    await update_member_nickname_with_title(interaction.user, user.active_title)
                await interaction.followup.send(f"✅ Đã đeo danh hiệu: `{sv_titles[choice]}`", ephemeral=True)
                return

            # Trang bị danh hiệu Custom
            if choice == "title_custom_equip":
                if not user.custom_title:
                    await interaction.followup.send("❌ Bạn chưa thiết lập danh hiệu tự chọn nào.", ephemeral=True)
                    return
                # Kiểm tra xem custom title có bị hết hạn hay chưa
                if user.custom_title_expiry and datetime.datetime.now() > user.custom_title_expiry:
                    await interaction.followup.send("❌ Danh hiệu tự chọn của bạn đã hết hạn 30 ngày. Vui lòng mua thẻ tùy biến để kích hoạt mới.", ephemeral=True)
                    return
                user.active_title = user.custom_title
                await session.commit()
                if isinstance(interaction.user, discord.Member):
                    await update_member_nickname_with_title(interaction.user, user.active_title)
                await interaction.followup.send(f"✅ Đã đeo danh hiệu tự chọn: `{user.custom_title}`", ephemeral=True)
                return



            # Trang bị màu dạ quang
            glow_colors = {
                "color_neon": ("Neon Pink", "Neon Pink"),
                "color_green": ("Hacker Green", "Hacker Green"),
                "color_red": ("Blood Red", "Blood Red")
            }
            if choice in glow_colors:
                # Kiểm tra sở hữu bảng tên dạ quang
                inv_res = await session.execute(
                    select(Inventory).filter_by(user_id=user_id, item_id="name_color")
                )
                inv = inv_res.scalar_one_or_none()
                if not inv or inv.quantity <= 0:
                    await interaction.followup.send("❌ Bạn phải có vật phẩm **Bảng Tên Dạ Quang** trong túi đồ để đổi màu.", ephemeral=True)
                    return

                color_name, role_name = glow_colors[choice]
                user.active_color = color_name
                user.chameleon_enabled = False
                await session.commit()
                
                if guild:
                    await remove_user_color_roles(interaction.user)
                    role = await ensure_color_role(guild, color_name)
                    if role:
                        try:
                            await interaction.user.add_roles(role)
                        except Exception:
                            await interaction.followup.send("⚠️ Đã lưu thiết lập màu nhưng Bot thiếu quyền quản lý role để gán màu tên thực tế cho bạn.", ephemeral=True)
                            return

                await interaction.followup.send(f"✅ Đã đổi màu tên của bạn thành: **{color_name}**", ephemeral=True)
                return

            # Trang bị hiệu ứng Tắc kè hoa
            if choice == "color_chameleon":
                # Kiểm tra sở hữu hiệu ứng tắc kè hoa
                inv_res = await session.execute(
                    select(Inventory).filter_by(user_id=user_id, item_id="chameleon")
                )
                inv = inv_res.scalar_one_or_none()
                if not inv or inv.quantity <= 0:
                    await interaction.followup.send("❌ Bạn phải sở hữu hiệu ứng **Tắc Kè Hoa** trong túi đồ.", ephemeral=True)
                    return

                user.active_color = "Chameleon"
                user.chameleon_enabled = True
                await session.commit()
                
                if guild:
                    await remove_user_color_roles(interaction.user)
                    role = await ensure_color_role(guild, "Chameleon")
                    if role:
                        try:
                            await interaction.user.add_roles(role)
                        except Exception:
                            await interaction.followup.send("⚠️ Đã bật Tắc Kè Hoa nhưng Bot thiếu quyền gán role thực tế cho bạn.", ephemeral=True)
                            return
                
                await interaction.followup.send("✅ Đã kích hoạt hiệu ứng **Tắc Kè Hoa** (Màu tên đổi liên tục hàng giờ).", ephemeral=True)
                return


    # ----------------- SỬ DỤNG VẬT PHẨM CHỨC NĂNG -----------------
    async def handle_inventory_item_use(self, interaction: discord.Interaction, item_id: str):
        """
        Điều hướng logic sử dụng từng vật phẩm.
        """
        # Nếu là các thẻ cần tương tác chọn mục tiêu
        if item_id in ["summon_card", "muzzle_card"]:
            await interaction.response.edit_message(
                content=f"Vui lòng chọn đối tượng bạn muốn sử dụng **{item_id.replace('_card', '').upper()}**:",
                embed=None,
                view=FriendSelectionView(item_id)
            )
            return

        # Nếu dùng Loa Phường, mở Modal nhập text
        if item_id == "loudspeaker":
            await interaction.response.send_modal(LoudspeakerModal())
            return

        # Nếu dùng các vật phẩm khác dùng trực tiếp
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        now = datetime.datetime.now()

        async with get_db_session() as session:
            # Kiểm tra số lượng trong túi
            inv_res = await session.execute(
                select(Inventory).filter_by(user_id=user_id, item_id=item_id)
            )
            inv = inv_res.scalar_one_or_none()
            if not inv or inv.quantity <= 0:
                await interaction.followup.send("❌ Bạn không có vật phẩm này.", ephemeral=True)
                return

            user_res = await session.execute(select(User).filter_by(user_id=user_id))
            user = user_res.scalar_one()

            # ☕ CÀ PHÊ ĐEN ĐÁ
            if item_id == "coffee":
                tracker_cog = self.bot.get_cog("Tracker")
                if not tracker_cog or user_id not in tracker_cog.active_sessions:
                    await interaction.followup.send("❌ Bạn phải đang trong **ca Focus hoạt động** mới có thể uống Cà Phê!", ephemeral=True)
                    return
                
                # Cộng 30 phút vào active session trong bộ nhớ đệm
                session_info = tracker_cog.active_sessions[user_id]
                session_info["duration"] += 30
                
                # Tiêu hao
                if inv.quantity > 1:
                    inv.quantity -= 1
                else:
                    await session.delete(inv)
                
                await session.commit()
                
                # Thông báo
                task_title = session_info["task_title"]
                await interaction.followup.send(
                    f"☕ **Bạn đã uống Cà Phê Đen Đá!**\n"
                    f"• Ca Focus cho mục tiêu `{task_title}` đã được cộng thêm **30 phút** mà không làm đứt quãng Flow tập trung của bạn!",
                    ephemeral=True
                )
                
                # Thử gửi thông báo vào voice channel của họ
                member = interaction.guild.get_member(user_id)
                if member and member.voice and member.voice.channel:
                    try:
                        await member.voice.channel.send(f"☕ {member.mention} đã nạp **Cà Phê Đen Đá**! Phiên làm việc kéo dài thêm 30 phút.")
                    except Exception:
                        pass
                return

            # 🧼 CỤC TẨY KHỔNG LỒ
            if item_id == "eraser":
                # Tìm vi phạm mới nhất
                violation_res = await session.execute(
                    select(ViolationLog)
                    .where(ViolationLog.user_id == user_id)
                    .order_by(ViolationLog.created_at.desc())
                    .limit(1)
                )
                violation = violation_res.scalar_one_or_none()
                if not violation:
                    await interaction.followup.send("❌ Bạn đang có một hồ sơ trong sạch, không có vết đen vi phạm nào cần tẩy xóa!", ephemeral=True)
                    return
                
                # Xóa vi phạm và tiêu hao
                await session.delete(violation)
                if inv.quantity > 1:
                    inv.quantity -= 1
                else:
                    await session.delete(inv)
                
                await session.commit()
                await interaction.followup.send("🧼 **Cục Tẩy Khổng Lồ đã hoạt động!**\n• Một vết đen vi phạm trong lịch sử kỷ luật của bạn đã được xóa bỏ hoàn toàn.", ephemeral=True)
                return

            # ⏳ THẺ X2 TỐC ĐỘ
            if item_id == "x2_speed":
                current_expiry = user.x2_expiry if (user.x2_expiry and user.x2_expiry > now) else now
                new_expiry = current_expiry + datetime.timedelta(hours=2)
                user.x2_expiry = new_expiry
                
                # Tiêu hao
                if inv.quantity > 1:
                    inv.quantity -= 1
                else:
                    await session.delete(inv)
                
                await session.commit()
                await interaction.followup.send(
                    f"⏳ **Kích hoạt Thẻ X2 Tốc Độ thành công!**\n"
                    f"• Bạn nhận được hiệu ứng nhân đôi EXP & Tokens khi Focus trong 2 tiếng tiếp theo.\n"
                    f"• Hết hạn lúc: `{new_expiry.strftime('%H:%M:%S ngày %d/%m/%Y')}`.",
                    ephemeral=True
                )
                return

            # 📦 MỞ RƯƠNG GÔ (Túi đồ)
            if item_id == "chest_wood":
                # Tiêu hao 1 rương
                if inv.quantity > 1:
                    inv.quantity -= 1
                else:
                    await session.delete(inv)
                await session.commit()
                
                # Chạy animation gacha mở rương gỗ
                await self.execute_open_wood_chest(interaction, user)
                return

            # 💎 MỞ RƯƠNG THỦY TINH (Túi đồ)
            if item_id == "chest_glass":
                # Tiêu hao 1 rương
                if inv.quantity > 1:
                    inv.quantity -= 1
                else:
                    await session.delete(inv)
                await session.commit()
                
                # Chạy animation gacha mở rương thủy tinh
                await self.execute_open_glass_chest(interaction, user)
                return


    # ----------------- THỰC THI SỬ DỤNG VẬT PHẨM MỤC TIÊU BẠN BÈ -----------------
    async def execute_interactive_item_use(self, interaction: discord.Interaction, item_id: str, target: discord.Member):
        """
        Xử lý Triệu Hồi (Summon) và Khóa Mõm (Muzzle) sau khi chọn mục tiêu.
        """
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        guild = interaction.guild
        now = datetime.datetime.now()

        if target.bot:
            await interaction.followup.send("❌ Không thể tương tác lên Bot!", ephemeral=True)
            return

        if target.id == user_id:
            await interaction.followup.send("❌ Bạn không thể tự chọn chính bản thân mình.", ephemeral=True)
            return

        async with get_db_session() as session:
            inv_res = await session.execute(
                select(Inventory).filter_by(user_id=user_id, item_id=item_id)
            )
            inv = inv_res.scalar_one_or_none()
            if not inv or inv.quantity <= 0:
                await interaction.followup.send("❌ Bạn không còn vật phẩm này trong túi đồ.", ephemeral=True)
                return

            user_res = await session.execute(select(User).filter_by(user_id=user_id))
            user = user_res.scalar_one()

            # 🥊 THẺ TRIỆU HỒI
            if item_id == "summon_card":
                # Thử gửi tin nhắn đầu tiên qua DM trước để đảm bảo mục tiêu có mở DM
                try:
                    await target.send(f"🥊 **[Triệu Hồi]** {target.mention} Dậy làm việc đi đồ lười biếng! (Triệu hồi bởi {interaction.user.mention})")
                except discord.Forbidden:
                    await interaction.followup.send(f"❌ Không thể triệu hồi {target.mention} vì họ đã khóa tin nhắn riêng (DM) từ người lạ.", ephemeral=True)
                    return
                except Exception as e:
                    await interaction.followup.send(f"❌ Không thể gửi tin nhắn riêng cho {target.mention}: {e}", ephemeral=True)
                    return

                # Tiêu hao thẻ
                if inv.quantity > 1:
                    inv.quantity -= 1
                else:
                    await session.delete(inv)
                await session.commit()

                await interaction.followup.send(f"🥊 Đã gửi triệu hồi khích tướng tới tin nhắn riêng (DM) của {target.mention}!", ephemeral=True)
                
                try:
                    await asyncio.sleep(2.0)
                    await target.send(f"🥊 **[Triệu Hồi]** {target.mention} Dậy làm việc đi đồ lười biếng! (Hệ thống gõ đầu lần 2)")
                    await asyncio.sleep(2.0)
                    await target.send(f"🥊 **[Triệu Hồi]** {target.mention} Hãy vào phòng Focus làm việc nghiêm túc ngay lập tức!")
                except Exception:
                    pass
                return

            # 🔇 THẺ KHÓA MÕM
            if item_id == "muzzle_card":
                # Kiểm tra giới hạn 1 ngày/lần
                if user.last_muzzle_used and (now - user.last_muzzle_used).total_seconds() < 86400:
                    time_left = 86400 - (now - user.last_muzzle_used).total_seconds()
                    hours, mins = int(time_left // 3600), int((time_left % 3600) // 60)
                    await interaction.followup.send(f"❌ Bạn đã dùng thẻ Khóa Mõm hôm nay rồi! Vui lòng đợi `{hours}g {mins}p` nữa để tránh gây toxic.", ephemeral=True)
                    return

                # Kiểm tra mục tiêu có trong phòng voice không
                if not target.voice or not target.voice.channel:
                    await interaction.followup.send("❌ Mục tiêu phải đang kết nối trong một kênh Voice mới có thể bị 'Khóa Mõm'!", ephemeral=True)
                    return

                # Tránh mute Admins/Mods
                if target.guild_permissions.administrator or target.guild_permissions.manage_roles:
                    await interaction.followup.send("❌ Bạn không thể 'Khóa Mõm' một Quản trị viên hoặc Điều hành viên!", ephemeral=True)
                    return

                # Thực hiện mute
                try:
                    await target.edit(mute=True, reason=f"Khóa Mõm bởi {interaction.user.name}")
                except discord.Forbidden:
                    await interaction.followup.send("❌ Bot thiếu quyền `Mute Members` để thực hiện khóa mõm thành viên.", ephemeral=True)
                    return
                except Exception as e:
                    logger.error(f"Lỗi khi cấm nói {target.name}: {e}")
                    await interaction.followup.send("❌ Gặp lỗi không xác định khi tắt mic mục tiêu.", ephemeral=True)
                    return

                # Tiêu hao thẻ và lưu mốc thời gian sử dụng
                if inv.quantity > 1:
                    inv.quantity -= 1
                else:
                    await session.delete(inv)
                user.last_muzzle_used = now
                await session.commit()

                await interaction.followup.send(f"🔇 **Khóa Mõm thành công!** Đã tắt mic của {target.mention} trong 60 giây.", ephemeral=True)
                
                # Send text thông báo công khai
                try:
                    await interaction.channel.send(f"🔇 **[Khóa Mõm]** {interaction.user.mention} đã khóa mõm {target.mention} trong đúng 60 giây! Hãy im lặng tập trung đi nhé.")
                except Exception:
                    pass

                # Tự động unmute sau 60 giây
                async def unmute_after_60_secs(member_to_unmute):
                    await asyncio.sleep(60)
                    try:
                        # Kiểm tra xem họ còn ở phòng voice không
                        if member_to_unmute.voice:
                            await member_to_unmute.edit(mute=False, reason="Hết hạn khóa mõm 60 giây")
                            # Gửi tin nhắn thông báo mở khóa
                            channel = interaction.channel
                            await channel.send(f"🔊 {member_to_unmute.mention} đã hết thời gian khóa mõm. Mic đã được mở lại!")
                    except Exception as e:
                        logger.error(f"Lỗi khi unmute lại cho {member_to_unmute.name}: {e}")

                asyncio.create_task(unmute_after_60_secs(target))
                return


    # ----------------- ĐẠI SIÊU THỊ: HIỂN THỊ CÁC PHÂN KHU -----------------
    async def handle_view_shop_categories(self, interaction: discord.Interaction):
        """
        Hiển thị 4 phân khu Grand Mall.
        """
        embed = discord.Embed(
            title="🏪 ĐẠI SIÊU THỊ CHRONOS (GRAND MALL) 🏪",
            description=(
                "Chào mừng đến với Grand Mall! Hãy lựa chọn phân khu bạn muốn ghé thăm từ Dropdown Menu phía dưới:\n\n"
                "🎭 **Phân khu 1: Cosmetics Store (Thời Trang)**\n"
                "*Cá nhân hóa Profile hiển thị và đổi màu tên cực ngầu trên Server.*\n\n"
                "🛠️ **Phân khu 2: Utility & Productivity (Năng suất & Sinh tồn)**\n"
                "*Gia tăng thời gian, nhân đôi EXP gặt hái hoặc xóa đi các vết đen lỗi lầm.*\n\n"
                "🎪 **Phân khu 3: Social Interactions (Quyền lực & Tương tác)**\n"
                "*Phát loa phường chat chung, triệu hồi bạn bè hoặc khóa mic troll đồng đội.*"
            ),
            color=discord.Color.green()
        )

        view = discord.ui.View(timeout=120)
        view.add_item(ShopCategorySelect())
        
        # Nút back
        back_btn = discord.ui.Button(label="⬅️ Quay lại Menu chính", style=discord.ButtonStyle.grey, row=1)
        async def back_callback(inter: discord.Interaction):
            embed_main = discord.Embed(
                title="🎒 HỆ THỐNG ĐẠI SIÊU THỊ & TÚI ĐỒ 🏪",
                description=(
                    "Hãy chọn một hành động trong Dropdown Menu phía dưới:\n\n"
                    "• **Túi đồ cá nhân:** Xem số dư và trang bị/sử dụng vật phẩm sở hữu.\n"
                    "• **Vào Đại Siêu Thị (Grand Mall):** Mua các vật phẩm và danh hiệu.\n"
                    "• **Tiệm cầm đồ (Pawn Shop):** Bán lại trang phục lấy Token khẩn cấp."
                ),
                color=discord.Color.blue()
            )
            await inter.response.edit_message(embed=embed_main, view=MainEconomyView())
        back_btn.callback = back_callback
        view.add_item(back_btn)

        await interaction.response.edit_message(embed=embed, view=view)


    # Hiển thị hàng hóa của từng phân khu cụ thể
    async def handle_view_shop_category_items(self, interaction: discord.Interaction, category: str):
        """
        Duyệt qua các vật phẩm trong phân khu được chọn.
        """


        async with get_db_session() as session:
            # Lọc theo loại vật phẩm
            if category == "cosmetic":
                res = await session.execute(select(Item).where(Item.item_type == "cosmetic", Item.is_gacha_only == False))
            elif category == "utility_survival":
                res = await session.execute(select(Item).where(Item.item_type.in_(["utility", "survival"]), Item.item_id.in_(["coffee", "rest_token", "x2_speed", "eraser"])))
            elif category == "social":
                res = await session.execute(select(Item).where(Item.item_type == "utility", Item.item_id.in_(["loudspeaker", "music_lyrical", "summon_card", "muzzle_card"])))
            
            items = res.scalars().all()

        category_titles = {
            "cosmetic": "🎭 PHÂN KHU 1: COSMETICS & THỜI TRANG 🎭",
            "utility_survival": "🛠️ PHÂN KHU 2: UTILITY & NĂNG SUẤT 🛠️",
            "social": "🎪 PHÂN KHU 3: SOCIAL & TƯƠNG TÁC 🎪"
        }

        embed = discord.Embed(
            title=category_titles.get(category, "🏪 CỬA HÀNG 🏪"),
            description="Lựa chọn một vật phẩm từ danh mục phía dưới và bấm thanh toán bằng Token:",
            color=discord.Color.green()
        )

        for item in items:
            embed.add_field(
                name=f"🛒 {item.name} — `{item.price} Tokens`",
                value=f"• **Mã ID:** `{item.item_id}`\n• *Mô tả:* {item.description}",
                inline=False
            )

        view = discord.ui.View(timeout=120)
        view.add_item(ShopItemSelect(items))
        
        # Quay lại Grand Mall
        back_btn = discord.ui.Button(label="⬅️ Quay lại Grand Mall", style=discord.ButtonStyle.grey, row=1)
        async def back_callback(inter: discord.Interaction):
            await self.handle_view_shop_categories(inter)
        back_btn.callback = back_callback
        view.add_item(back_btn)

        await interaction.response.edit_message(embed=embed, view=view)


    # ----------------- CHI TIẾT MUA HÀNG & THANH TOÁN -----------------
    async def handle_shop_item_details(self, interaction: discord.Interaction, item_id: str):
        async with get_db_session() as session:
            result = await session.execute(select(Item).filter_by(item_id=item_id))
            item = result.scalar_one_or_none()

        if not item:
            await interaction.response.send_message("❌ Không tìm thấy vật phẩm này.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Xác nhận thanh toán",
            description=(
                f"Bạn có chắc chắn muốn mua vật phẩm sau đây?\n\n"
                f"• **Tên vật phẩm:** {item.name}\n"
                f"• **Số lượng:** `1`\n"
                f"• **Tổng thanh toán:** `{item.price} Tokens`\n"
                f"• **Chi tiết:** *{item.description}*"
            ),
            color=discord.Color.green()
        )
        
        await interaction.response.edit_message(embed=embed, view=PurchaseConfirmationView(item_id, item.price))

    async def update_purchase_details_message(self, interaction: discord.Interaction, view: PurchaseConfirmationView):
        async with get_db_session() as session:
            result = await session.execute(select(Item).filter_by(item_id=view.item_id))
            item = result.scalar_one_or_none()

        if not item:
            await interaction.response.send_message("❌ Không tìm thấy vật phẩm này.", ephemeral=True)
            return

        total_price = item.price * view.quantity
        embed = discord.Embed(
            title="Xác nhận thanh toán",
            description=(
                f"Bạn có chắc chắn muốn mua vật phẩm sau đây?\n\n"
                f"• **Tên vật phẩm:** {item.name}\n"
                f"• **Số lượng:** `{view.quantity}`\n"
                f"• **Tổng thanh toán:** `{total_price} Tokens` (`{item.price} Tokens` / 1 sản phẩm)\n"
                f"• **Chi tiết:** *{item.description}*"
            ),
            color=discord.Color.green()
        )
        
        await interaction.response.edit_message(embed=embed, view=view)

    async def execute_purchase(self, interaction: discord.Interaction, item_id: str, price: int, quantity: int = 1):
        user_id = interaction.user.id
        total_price = price * quantity
        async with get_db_session() as session:
            user_res = await session.execute(select(User).filter_by(user_id=user_id))
            user = user_res.scalar_one_or_none()
            
            if not user or user.token_balance < total_price:
                current_tokens = user.token_balance if user else 0
                await interaction.response.edit_message(
                    content=f"❌ **Không đủ số dư!** Bạn cần `{total_price} Tokens` để mua `{quantity}` vật phẩm, hiện tại bạn chỉ có `{current_tokens} Tokens`.",
                    embed=None,
                    view=BackToMainView()
                )
                return

            # Khấu trừ Token
            user.token_balance -= total_price

            # Mua gói Title Sinh viên hoặc các vật phẩm gỡ/mở khóa đặc biệt
            if item_id == "title_student":
                user.unlocked_student_titles = True
            
            # Thêm vật phẩm vào túi đồ (đối với những item giữ số lượng)
            inv_res = await session.execute(
                select(Inventory).filter_by(user_id=user_id, item_id=item_id)
            )
            inv = inv_res.scalar_one_or_none()
            if inv:
                inv.quantity += quantity
            else:
                new_inv = Inventory(user_id=user_id, item_id=item_id, quantity=quantity)
                session.add(new_inv)

            await session.commit()
            new_balance = user.token_balance

            # Lấy tên vật phẩm cho thông báo đẹp hơn
            item_res = await session.execute(select(Item).filter_by(item_id=item_id))
            item_db = item_res.scalar_one_or_none()
            item_name = item_db.name if item_db else item_id

        await interaction.response.edit_message(
            content=f"🎉 **Mua sắm thành công!** Bạn đã sở hữu **{quantity}x {item_name}**. Số dư mới: `{new_balance} Tokens`.",
            embed=None,
            view=BackToMainView()
        )


    # ----------------- TIỆM CẦM ĐỒ -----------------
    async def handle_view_pawn(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        async with get_db_session() as session:
            # Lấy các vật phẩm trang trí (cosmetic)
            res = await session.execute(
                select(Inventory, Item)
                .join(Item, Inventory.item_id == Item.item_id)
                .where(Inventory.user_id == user_id, Item.item_type == "cosmetic")
            )
            inventory_cosmetics = res.all()

        embed = discord.Embed(
            title="🤝 TIỆM CẦM ĐỒ CHRONOS 🤝",
            description=(
                "Tại đây bạn có thể thanh lý lại các Danh hiệu & Hiệu ứng (`cosmetic`) không dùng tới để nhận lại **50% giá trị gốc bằng Token**.\n\n"
                "💡 *Gỡ rối Token khẩn cấp khi bạn đang cần cày cuốc cứu chuỗi streak kỷ luật!*"
            ),
            color=discord.Color.dark_red()
        )

        if not inventory_cosmetics:
            embed.add_field(
                name="Trạng thái",
                value="*Bạn không sở hữu vật phẩm trang trí (Cosmetics) nào có thể bán lại lúc này!*",
                inline=False
            )
            await interaction.response.edit_message(embed=embed, view=BackToMainView())
        else:
            pawn_list_str = ""
            for inv, item in inventory_cosmetics:
                base_price = item.price if item.price > 0 else 50 # Rare title tính mốc 50
                sell_price = int(base_price * 0.5)
                pawn_list_str += f"• **{item.name}** — Nhận lại: `{sell_price}` 🪙 (Số lượng: x{inv.quantity})\n"
            
            embed.add_field(name="Vật phẩm có thể thanh lý", value=pawn_list_str, inline=False)
            
            view = discord.ui.View()
            view.add_item(PawnItemSelect(inventory_cosmetics))
            
            # Nút back
            back_btn = discord.ui.Button(label="⬅️ Quay lại", style=discord.ButtonStyle.grey, row=1)
            async def back_callback(inter: discord.Interaction):
                embed_main = discord.Embed(
                    title="🎒 HỆ THỐNG ĐẠI SIÊU THỊ & TÚI ĐỒ 🏪",
                    description="Hãy chọn một hành động...",
                    color=discord.Color.blue()
                )
                await inter.response.edit_message(embed=embed_main, view=MainEconomyView())
            back_btn.callback = back_callback
            view.add_item(back_btn)

            await interaction.response.edit_message(embed=embed, view=view)

    async def handle_pawn_sell_confirm(self, interaction: discord.Interaction, item_id: str):
        async with get_db_session() as session:
            res = await session.execute(select(Item).filter_by(item_id=item_id))
            item = res.scalar_one()
            base_price = item.price if item.price > 0 else 50
            sell_price = int(base_price * 0.5)

        embed = discord.Embed(
            title="Xác nhận bán lại",
            description=(
                f"Bạn có chắc chắn muốn thanh lý vật phẩm này?\n\n"
                f"• **Vật phẩm:** {item.name}\n"
                f"• **Nhận lại:** `{sell_price} Tokens` (50% giá gốc: {base_price})"
            ),
            color=discord.Color.red()
        )
        await interaction.response.edit_message(embed=embed, view=SellConfirmationView(item_id, sell_price))

    async def execute_sell(self, interaction: discord.Interaction, item_id: str, sell_price: int):
        user_id = interaction.user.id
        async with get_db_session() as session:
            # Kiểm tra túi đồ
            inv_res = await session.execute(
                select(Inventory).filter_by(user_id=user_id, item_id=item_id)
            )
            inv = inv_res.scalar_one_or_none()
            
            if not inv or inv.quantity <= 0:
                await interaction.response.edit_message(
                    content="❌ Bạn không còn vật phẩm này để thanh lý.",
                    embed=None,
                    view=BackToMainView()
                )
                return

            # Khấu trừ số lượng
            if inv.quantity > 1:
                inv.quantity -= 1
            else:
                await session.delete(inv)

            # Nếu bán title hiếm hoặc gỡ bỏ student titles
            user_res = await session.execute(select(User).filter_by(user_id=user_id))
            user = user_res.scalar_one()
            

            if item_id == "title_student":
                user.unlocked_student_titles = False
                if user.active_title in ["Đẹp trai có gì sai", "Hôm nay tôi buồn", "Chúa tể chạy Deadline", "Kẻ thù của giường ngủ"]:
                    user.active_title = None
            elif item_id == "title_custom":
                user.custom_title = None
                user.custom_title_expiry = None
                if user.active_title == user.custom_title:
                    user.active_title = None

            # Cộng Token
            user.token_balance += sell_price
            await session.commit()
            new_balance = user.token_balance

        await interaction.response.edit_message(
            content=f"🎉 **Thanh lý thành open!** Đã thu hồi `{item_id}` và hoàn lại `+{sell_price} Tokens`. Số dư hiện tại: `{new_balance} Tokens`.",
            embed=None,
            view=BackToMainView()
        )





    # ----------------- VOICE JOIN LISTENER: NHẠC TRỮ TÌNH -----------------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """
        Lắng nghe khi người dùng kết nối voice. Nếu họ sở hữu và đang bật hiệu ứng Nhạc Trữ Tình,
        bot sẽ bay vào bật nhạc chào mừng rồi thoát.
        """
        if member.bot:
            return

        # Chỉ kích hoạt khi user mới vào kênh thoại (không tính tắt mic/màn hình)
        if before.channel is None and after.channel is not None:
            # Bỏ qua kênh trigger tạo phòng động để tránh tranh chấp phòng
            if after.channel.id == config.FOCUS_TRIGGER_CHANNEL_ID:
                return

            user_id = member.id
            async with get_db_session() as session:
                # Kiểm tra xem có Nhạc Trữ Tình trong túi đồ không
                inv_res = await session.execute(
                    select(Inventory).filter_by(user_id=user_id, item_id="music_lyrical")
                )
                inv = inv_res.scalar_one_or_none()
                if not inv or inv.quantity <= 0:
                    return

            logger.info(f"User {member.name} gia nhập Voice và sở hữu Nhạc Trữ Tình. Kích hoạt bot chào mừng...")

            # Kết nối vào kênh voice của họ
            try:
                guild = after.channel.guild
                
                # Tránh xung đột nếu bot đang ở phòng voice khác
                if guild.voice_client is not None:
                    return

                vc = await after.channel.connect()

                # Gửi thông báo fallback đẹp mắt
                try:
                    await after.channel.send(f"🎶 **[Nhạc Trữ Tình]** Đang phát nhạc chào mừng John Cena cực kỳ hoành tráng cho {member.mention}! 🎶")
                except Exception:
                    pass

                # Thử phát nhạc nếu có sẵn file nhạc và ffmpeg
                if os.path.exists("entry.wav"):
                    try:
                        source = discord.FFmpegPCMAudio("entry.wav")
                        vc.play(source)
                    except Exception as play_error:
                        logger.warning(f"Không thể chạy trình phát nhạc voice (thiếu ffmpeg?): {play_error}")

                # Chờ 5 giây rồi thoát
                await asyncio.sleep(5)
                await vc.disconnect()

            except Exception as e:
                logger.error(f"Lỗi khi xử lý phát nhạc voice chào mừng cho {member.name}: {e}")


    # ----------------- HYBRID / SLASH COMMANDS -----------------

    @commands.hybrid_command(name="summon", description="🥊 Triệu hồi bạn bè lười biếng vào phòng Focus (Tiêu hao 1 Thẻ Triệu Hồi)")
    @discord.app_commands.describe(target="Người bạn muốn triệu hồi")
    async def summon(self, ctx: commands.Context, target: discord.Member):
        """
        Lệnh triệu hồi bạn bè.
        """
        user_id = ctx.author.id
        now = datetime.datetime.now()

        if target.bot:
            await ctx.send("❌ Không thể triệu hồi Bot!", ephemeral=True)
            return

        if target.id == user_id:
            await ctx.send("❌ Bạn không thể tự triệu hồi chính mình.", ephemeral=True)
            return

        async with get_db_session() as session:
            inv_res = await session.execute(
                select(Inventory).filter_by(user_id=user_id, item_id="summon_card")
            )
            inv = inv_res.scalar_one_or_none()
            if not inv or inv.quantity <= 0:
                await ctx.send("❌ Bạn không có **Thẻ Triệu Hồi** trong túi đồ. Hãy vào shop mua sắm!", ephemeral=True)
                return

            # Thử gửi tin nhắn đầu tiên qua DM trước để đảm bảo mục tiêu có mở DM
            try:
                await target.send(f"🥊 **[Triệu Hồi]** {target.mention} Dậy làm việc đi đồ lười biếng! (Triệu hồi bởi {ctx.author.mention})")
            except discord.Forbidden:
                await ctx.send(f"❌ Không thể triệu hồi {target.mention} vì họ đã khóa tin nhắn riêng (DM) từ người lạ.", ephemeral=True)
                return
            except Exception as e:
                await ctx.send(f"❌ Không thể gửi tin nhắn riêng cho {target.mention}: {e}", ephemeral=True)
                return

            # Tiêu hao
            if inv.quantity > 1:
                inv.quantity -= 1
            else:
                await session.delete(inv)
            await session.commit()

        await ctx.send(f"🥊 Đã gửi triệu hồi khích tướng tới tin nhắn riêng (DM) của {target.mention}!", ephemeral=True)
        try:
            await asyncio.sleep(2.0)
            await target.send(f"🥊 **[Triệu Hồi]** {target.mention} Dậy làm việc đi đồ lười biếng! (Hệ thống gõ đầu lần 2)")
            await asyncio.sleep(2.0)
            await target.send(f"🥊 **[Triệu Hồi]** {target.mention} Hãy vào phòng Focus làm việc nghiêm túc ngay lập tức!")
        except Exception:
            pass

    @commands.hybrid_command(name="mutefriend", description="🔇 Khóa mõm (Tắt mic voice) bạn bè trong 60 giây (Tiêu hao 1 Thẻ Khóa Mõm, 1 lần/ngày)")
    @discord.app_commands.describe(target="Người bạn muốn tắt mic")
    async def mutefriend(self, ctx: commands.Context, target: discord.Member):
        """
        Lệnh khóa mõm bạn bè trong phòng voice.
        """
        user_id = ctx.author.id
        now = datetime.datetime.now()

        if target.bot:
            await ctx.send("❌ Không thể khóa mõm Bot!", ephemeral=True)
            return

        if target.id == user_id:
            await ctx.send("❌ Bạn không thể tự khóa mõm chính mình.", ephemeral=True)
            return

        async with get_db_session() as session:
            inv_res = await session.execute(
                select(Inventory).filter_by(user_id=user_id, item_id="muzzle_card")
            )
            inv = inv_res.scalar_one_or_none()
            if not inv or inv.quantity <= 0:
                await ctx.send("❌ Bạn không có **Thẻ Khóa Mõm** trong túi đồ. Hãy mua tại Shop!", ephemeral=True)
                return

            user_res = await session.execute(select(User).filter_by(user_id=user_id))
            user = user_res.scalar_one()

            # Giới hạn 1 ngày/lần
            if user.last_muzzle_used and (now - user.last_muzzle_used).total_seconds() < 86400:
                time_left = 86400 - (now - user.last_muzzle_used).total_seconds()
                hours, mins = int(time_left // 3600), int((time_left % 3600) // 60)
                await ctx.send(f"❌ Bạn đã dùng thẻ Khóa Mõm hôm nay rồi! Vui lòng đợi `{hours}g {mins}p` để tiếp tục sử dụng.", ephemeral=True)
                return

            # Kiểm tra voice
            if not target.voice or not target.voice.channel:
                await ctx.send("❌ Người này phải đang kết nối trong một kênh Voice mới có thể bị 'Khóa Mõm'!", ephemeral=True)
                return

            # Tránh cấm Admins/Mods
            if target.guild_permissions.administrator or target.guild_permissions.manage_roles:
                await ctx.send("❌ Bạn không thể 'Khóa Mõm' một Quản trị viên hoặc Điều hành viên!", ephemeral=True)
                return

            # Thực hiện
            try:
                await target.edit(mute=True, reason=f"Khóa Mõm bởi {ctx.author.name}")
            except discord.Forbidden:
                await ctx.send("❌ Bot thiếu quyền `Mute Members` để thực hiện khóa mõm thành viên.", ephemeral=True)
                return
            except Exception as e:
                logger.error(f"Lỗi khi cấm nói {target.name}: {e}")
                await ctx.send("❌ Không thể tắt mic của mục tiêu lúc này.", ephemeral=True)
                return

            # Khấu trừ thẻ và lưu mốc thời gian
            if inv.quantity > 1:
                inv.quantity -= 1
            else:
                await session.delete(inv)
            user.last_muzzle_used = now
            await session.commit()

        await ctx.send(f"🔇 **Khóa Mõm thành công!** Đã tắt mic của {target.mention} trong 60 giây.", ephemeral=True)
        try:
            await ctx.channel.send(f"🔇 **[Khóa Mõm]** {ctx.author.mention} đã tắt tiếng {target.mention} trong 60 giây để yêu cầu yên lặng tập trung!")
        except Exception:
            pass

        async def unmute_after_60_secs(member_to_unmute):
            await asyncio.sleep(60)
            try:
                if member_to_unmute.voice:
                    await member_to_unmute.edit(mute=False, reason="Hết hạn khóa mõm 60 giây")
                    await ctx.channel.send(f"🔊 {member_to_unmute.mention} đã hết thời gian khóa mõm. Mic đã được mở lại!")
            except Exception as e:
                logger.error(f"Lỗi khi unmute cho {member_to_unmute.name}: {e}")

        asyncio.create_task(unmute_after_60_secs(target))

    @commands.hybrid_command(name="loa", description="📢 Pin 1 tin nhắn lên kênh Chat Chung trong 1 tiếng (Tiêu hao 1 Loa Phường)")
    @discord.app_commands.describe(message="Nội dung thông điệp bạn muốn ghim")
    async def loa(self, ctx: commands.Context, *, message: str):
        """
        Lệnh phát Loa Phường.
        """
        user_id = ctx.author.id

        async with get_db_session() as session:
            inv_res = await session.execute(
                select(Inventory).filter_by(user_id=user_id, item_id="loudspeaker")
            )
            inv = inv_res.scalar_one_or_none()
            if not inv or inv.quantity <= 0:
                await ctx.send("❌ Bạn không có **Loa Phường** trong túi đồ. Hãy mua tại Shop!", ephemeral=True)
                return

            # Tiêu hao
            if inv.quantity > 1:
                inv.quantity -= 1
            else:
                await session.delete(inv)
            await session.commit()

        guild = ctx.guild
        channel = guild.get_channel(config.KENH_THONG_BAO_ID) if config.KENH_THONG_BAO_ID else None
        if not channel:
            channel = (
                discord.utils.get(guild.text_channels, name="chat-chung") or 
                discord.utils.get(guild.text_channels, name="general") or 
                ctx.channel
            )

        embed = discord.Embed(
            title="📢 BẢN TIN LOA PHƯỜNG 📢",
            description=f"## ❝ **{message}** ❞\n\n*(Phát thanh từ {ctx.author.mention} — Ghim nổi bật trong 1 giờ)*",
            color=discord.Color.red(),
            timestamp=datetime.datetime.now()
        )
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)

        try:
            sent_msg = await channel.send(embed=embed)
            await sent_msg.pin()

            # Unpin sau 1 giờ
            async def unpin_later(msg):
                await asyncio.sleep(3600)
                try:
                    await msg.unpin()
                except Exception:
                    pass
            asyncio.create_task(unpin_later(sent_msg))

            await ctx.send(f"📢 Đã phát và ghim tin nhắn Loa Phường thành công tại kênh {channel.mention}!", ephemeral=True)
        except Exception as e:
            logger.error(f"Lỗi khi phát loa phường: {e}")
            await ctx.send("❌ Bot thiếu quyền để gửi hoặc ghim tin nhắn ở kênh Chat Chung.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Economy(bot))
