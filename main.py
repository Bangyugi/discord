import os
import asyncio
import logging
import discord
from discord.ext import commands
from aiohttp import web
import config
from database.db_session import init_db

# Web server keep-alive handler và hàm khởi động cho các host như Render
async def handle_ping(request):
    return web.Response(text="Chronos Bot is active and running 24/7!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render cung cấp cổng qua biến môi trường PORT, mặc định chạy ở port 10000 nếu không có
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.getLogger("ChronosBot").info(f"Web server Keep-Alive đã chạy tại cổng {port}")


# Thiết lập hệ thống logging ghi log ra console và định dạng dễ theo dõi
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ChronosBot")

class ChronosBot(commands.Bot):
    def __init__(self):
        # Cấu hình các Intents cần thiết để theo dõi voice state và tương tác tin nhắn
        intents = discord.Intents.default()
        intents.members = True          # Theo dõi thành viên join/leave, role update
        intents.voice_states = True     # Rất quan trọng để theo dõi phòng thoại động
        intents.message_content = True  # Đọc nội dung tin nhắn nếu có dùng prefix commands
        
        super().__init__(
            command_prefix="!", 
            intents=intents, 
            help_command=None
        )

    async def setup_hook(self):
        """
        setup_hook được gọi bất đồng bộ trước khi bot đăng nhập vào Discord.
        Đây là nơi lý tưởng để khởi tạo database và tải các Cogs.
        """
        # 1. Khởi tạo cơ sở dữ liệu (tự động tạo bảng nếu chưa có)
        try:
            await init_db()
        except Exception as e:
            logger.critical(f"Không thể khởi tạo cơ sở dữ liệu: {e}")
            raise e

        # 2. Tự động quét và tải toàn bộ Cogs trong thư mục cogs/
        cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
        for filename in os.listdir(cogs_dir):
            if filename.endswith(".py") and filename != "__init__.py":
                cog_name = f"cogs.{filename[:-3]}"
                try:
                    await self.load_extension(cog_name)
                    logger.info(f"Đã tải Cog thành công: {cog_name}")
                except Exception as e:
                    logger.error(f"Lỗi khi tải Cog {cog_name}: {e}")

        # 3. Khởi chạy Web Server Keep-Alive cho hosting Render/PaaS
        try:
            await start_web_server()
        except Exception as e:
            logger.error(f"Lỗi khi khởi chạy Web Server Keep-Alive: {e}")

    async def on_ready(self):
        logger.info(f"=========================================")
        logger.info(f"Bot {self.user.name}#{self.user.discriminator} (ID: {self.user.id}) đã ONLINE!")
        logger.info(f"Đang hoạt động trên {len(self.guilds)} server(s).")
        logger.info(f"=========================================")

        # Đồng bộ hóa slash commands (app_commands) với Discord
        try:
            synced = await self.tree.sync()
            logger.info(f"Đã đồng bộ {len(synced)} slash commands thành công.")
        except Exception as e:
            logger.error(f"Lỗi khi đồng bộ slash commands: {e}")

async def main():
    # Khởi tạo thực thể bot
    bot = ChronosBot()
    
    # Chạy bot với token từ file cấu hình
    async with bot:
        await bot.start(config.BOT_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot đang dừng theo yêu cầu của người dùng...")
    except Exception as e:
        logger.critical(f"Lỗi hệ thống nghiêm trọng: {e}")
