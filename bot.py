"""
==================================================
FREAKOS Discord Bot — Master Entry Point
==================================================
Production-ready, public, multi-server Discord bot.
Built with Python 3.11+ and discord.py 2.x.
"""
import os
import sys
import logging
from datetime import datetime
import discord
from discord.ext import commands
from dotenv import load_dotenv

from services.database import DatabaseManager
from services.logging_service import LoggingService
from services.scheduler import BackgroundScheduler
from utils.errors import setup_error_handlers
from cogs import COGS_LIST

# Load environment variables
load_dotenv()

# Configure Logging
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/freakos.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("freakos.main")

class FreakosBot(commands.Bot):
    """
    FREAKOS Master Bot Client.
    Manages services, database lifecycle, persistent views, and cog loading.
    """

    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.message_content = True
        intents.voice_states = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            activity=discord.Activity(type=discord.ActivityType.watching, name="servers | /help"),
            status=discord.Status.online
        )

        db_path = os.getenv("DATABASE_PATH", "data/freakos.db")
        self.db = DatabaseManager(db_path=db_path)
        self.logging_service = LoggingService(self)
        self.scheduler = BackgroundScheduler(self)

    async def setup_hook(self):
        """Called automatically before the bot connects to Discord."""
        logger.info("Starting FREAKOS initialization sequence...")

        # 1. Connect and initialize persistent database
        await self.db.connect()

        # 2. Register global error handlers
        setup_error_handlers(self)

        # 3. Load all feature cogs
        for cog_name in COGS_LIST:
            try:
                await self.load_extension(cog_name)
                logger.info(f"Loaded extension: {cog_name}")
            except Exception as e:
                logger.error(f"Failed to load extension {cog_name}: {e}", exc_info=True)

        # 4. Register static persistent views
        from cogs.tickets import TicketControlView, TicketPanelView
        from cogs.vouches import VouchPanelView
        from cogs.shop import ShopPanelView
        from cogs.giveaways import GiveawayEntryView

        self.add_view(TicketControlView())
        self.add_view(TicketPanelView())
        self.add_view(VouchPanelView())
        self.add_view(ShopPanelView())
        self.add_view(GiveawayEntryView())

        # 5. Register dynamic persistent reaction role views from database
        try:
            from cogs.reaction_roles import ReactionRoleDynamicButtonView
            panels = await self.db.get_all_reaction_panels()
            for p in panels:
                items = await self.db.get_reaction_items(p["panel_id"])
                if items:
                    self.add_view(ReactionRoleDynamicButtonView(p["panel_id"], items))
            logger.info(f"Restored {len(panels)} persistent reaction role panel views.")
        except Exception as e:
            logger.error(f"Error restoring reaction role views: {e}")

        # 6. Start background async scheduler for recovery
        self.scheduler.start()

        # 7. Slash Command Synchronization
        dev_guild_id = os.getenv("DEV_GUILD_ID", "").strip()
        try:
            if dev_guild_id and dev_guild_id.isdigit():
                dev_guild = discord.Object(id=int(dev_guild_id))
                self.tree.copy_global_to(guild=dev_guild)
                synced = await self.tree.sync(guild=dev_guild)
                logger.info(f"Synced {len(synced)} slash commands to DEV GUILD ({dev_guild_id}).")
            else:
                synced = await self.tree.sync()
                logger.info(f"Globally synced {len(synced)} slash commands.")
        except Exception as e:
            logger.error(f"Error syncing slash commands: {e}", exc_info=True)

    async def on_ready(self):
        """Fired when bot is connected and ready."""
        logger.info("==================================================")
        logger.info(f"FREAKOS BOT ONLINE: {self.user} (ID: {self.user.id})")
        logger.info(f"Connected Guilds: {len(self.guilds)}")
        logger.info("==================================================")

    async def close(self):
        """Cleanup upon bot shutdown."""
        logger.info("Shutting down FREAKOS bot...")
        self.scheduler.stop()
        await self.db.close()
        await super().close()

def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token or token == "your_discord_bot_token_here":
        logger.critical(
            "Missing DISCORD_TOKEN in environment!\n"
            "Please create a .env file based on .env.example and set your DISCORD_TOKEN."
        )
        sys.exit(1)

    bot = FreakosBot()
    bot.run(token, log_handler=None)

if __name__ == "__main__":
    main()
