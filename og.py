import discord
from discord.ext import commands
import json
import asyncio
import aiofiles
import random
import datetime
import psutil
import os
import logging
from typing import List, Dict, Set, Optional, Tuple
import threading

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

MAINIDD = 1413352398922973235
WLCMID = 1479676935683575963
FLOOD_CHANNEL_ID = 1482030865650286615
COOLDOWN_DURATION = 10
OWNER_IDS = {1412756800767393873}
MAX_DMS_PER_BOT = 10  # Each bot sends at most 10 DMs per flood
EXTRA_BOT_STARTUP_DELAY = 3  # Seconds to wait for extra bots to connect before flooding



class FloodBot(commands.Bot):
    def __init__(self, token: str):
        super().__init__(command_prefix=".", intents=discord.Intents.all(),owner_ids=[1412756800767393873])
        self.token = token
        self.secure_users: Set[int] = set()
        self.start_time = datetime.datetime.now()
        self.dm_count = 0
        self.dm_lock = asyncio.Lock()

    async def setup_hook(self):
        self.secure_users = await self.load_secure_users()
        logger.info(f"Bot {self.user.id} Ready!")

    async def load_secure_users(self) -> Set[int]:
        try:
            async with aiofiles.open('secure.json', 'r') as f:
                content = await f.read()
                return set(json.loads(content))
        except FileNotFoundError:
            async with aiofiles.open('secure.json', 'w') as f:
                await f.write('[]')
            return set()

    async def save_secure_users(self):
        async with aiofiles.open('secure.json', 'w') as f:
            await f.write(json.dumps(list(self.secure_users)))

    async def increment_dm_count(self):
        async with self.dm_lock:
            self.dm_count += 1

class BotManager:
    def __init__(self):
        self.bots: List[FloodBot] = []
        self.main_bot: FloodBot = None
        self.main_token: Optional[str] = None
        self.extra_tokens: List[str] = []
        self.valid_tokens: List[str] = []
        self.invalid_tokens: List[str] = []
        self.flood_cooldowns: Dict[int, float] = {}
        self.raid_mode: bool = False
        self.total_dms = 0
        self.start_time = None

    async def validate_token(self, token: str) -> Tuple[bool, Optional[int]]:
        """Validate token and return (is_valid, user_id)."""
        try:
            bot = FloodBot(token)
            await bot.login(token)
            uid = bot.user.id
            await bot.close()
            return True, uid
        except (discord.errors.LoginFailure, discord.errors.HTTPException):
            return False, None
        except Exception as e:
            logger.error(f"Unknown error validating token: {str(e)}")
            return False, None

    async def load_tokens(self) -> bool:
        try:
            async with aiofiles.open('tokens.txt', 'r') as f:
                tokens = await f.readlines()
            tokens = [token.strip() for token in tokens if token.strip()]
            
            logger.info(f"Found {len(tokens)} tokens, validating...")
            
            tasks = [self.validate_token(token) for token in tokens]
            validation_results = await asyncio.gather(*tasks)
            
            for token, (is_valid, uid) in zip(tokens, validation_results):
                if is_valid:
                    self.valid_tokens.append(token)
                    logger.info(f"Token valid: {token[:20]}... (ID: {uid})")
                    if uid == MAINIDD:
                        self.main_token = token
                    else:
                        self.extra_tokens.append(token)
                else:
                    self.invalid_tokens.append(token)
                    logger.info(f"Token invalid: {token[:20]}...")

            logger.info(f"\nValidation complete:")
            logger.info(f"Valid tokens: {len(self.valid_tokens)}")
            logger.info(f"Invalid tokens: {len(self.invalid_tokens)}")
            logger.info(f"Main bot token found: {self.main_token is not None}")
            logger.info(f"Extra bot tokens: {len(self.extra_tokens)}")

            # Fallback: if MAINIDD not matched, treat the first valid token as main
            if not self.main_token and self.valid_tokens:
                self.main_token = self.valid_tokens[0]
                self.extra_tokens = self.valid_tokens[1:]
                logger.warning("Main bot not identified by MAINIDD; using first valid token as main.")
            
            if self.invalid_tokens:
                async with aiofiles.open('invalid_tokens.txt', 'w') as f:
                    await f.write('\n'.join(self.invalid_tokens))
                logger.info("Invalid tokens have been saved to 'invalid_tokens.txt'")
                
        except FileNotFoundError:
            logger.error("tokens.txt not found!")
            return False
        except Exception as e:
            logger.error(f"Error loading tokens: {str(e)}")
            return False
            
        return bool(self.main_token)

    async def load_premium_users(self) -> Set[int]:
        try:
            async with aiofiles.open('premium.json', 'r') as f:
                content = await f.read()
                return set(json.loads(content))
        except FileNotFoundError:
            async with aiofiles.open('premium.json', 'w') as f:
                await f.write('[]')
            return set()

    async def save_premium_users(self, premium_users: Set[int]):
        async with aiofiles.open('premium.json', 'w') as f:
            await f.write(json.dumps(list(premium_users)))

    async def start_extra_bots(self) -> Tuple[List[FloodBot], List[asyncio.Task]]:
        """Start extra bots for flooding. Returns (extra_bots, tasks)."""
        extra_bots: List[FloodBot] = []
        tasks: List[asyncio.Task] = []
        for token in self.extra_tokens:
            bot = FloodBot(token)
            await self.setup_bot_commands(bot)
            extra_bots.append(bot)
            self.bots.append(bot)
            task = asyncio.create_task(bot.start(token))
            tasks.append(task)
        if extra_bots:
            # Allow extra bots time to connect before flooding
            await asyncio.sleep(EXTRA_BOT_STARTUP_DELAY)
        return extra_bots, tasks

    async def stop_extra_bots(self, extra_bots: List[FloodBot], tasks: List[asyncio.Task]):
        """Stop and remove extra bots after flooding."""
        for bot in extra_bots:
            try:
                self.bots.remove(bot)
            except ValueError:
                pass
            try:
                await bot.close()
            except Exception:
                pass
        for task in tasks:
            if not task.done():
                task.cancel()

    async def setup_bot_commands(self, bot: FloodBot):
        bot.remove_command("help")
        @bot.event
        async def on_ready():
            logger.info(f"Bot {bot.user.id} Online!")
            activity = discord.Game(name="Flooding DMs | .secure to protect")  
            await bot.change_presence(activity=activity)
            if bot.user.id == MAINIDD:
                try:
                    await bot.load_extension("jishaku")
                    logger.info("jishaku extension loaded.")
                except Exception as e:
                    logger.error(f"Failed to load jishaku: {str(e)}")

        @bot.event
        async def on_member_join(member):
            if bot.user.id == MAINIDD:
                welcome_channel = bot.get_channel(WLCMID)
                if welcome_channel:
                    embed = discord.Embed(description=f"Welcome {member.mention} to the server!")
                    await welcome_channel.send(embed=embed)

            if bot.user.id == MAINIDD: 
                role = discord.utils.get(member.guild.roles, id=1482030924714610860)  
                if role:
                    await member.add_roles(role)  
                    logger.info(f"Assigned role {role.name} to {member.id}")
                else:
                    logger.error("Role with ID 1482030924714610860 not found.")

        @bot.command(name="stats")
        async def stats(ctx):
            if bot.user.id != MAINIDD:
                return

            try:
                with open("secure.json", "r") as f:
                    secured_users_list = json.load(f)
            except FileNotFoundError:
                secured_users_list = []

            secured_users = len(secured_users_list)
            cpu_percent = psutil.cpu_percent()
            memory = psutil.Process().memory_info().rss / 1024 / 1024
            ping = round(bot.latency * 1000)
            bot_uptime = datetime.datetime.now() - bot.start_time
            days, remainder = divmod(bot_uptime.total_seconds(), 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, seconds = divmod(remainder, 60)
            formatted_uptime = f"{int(days)} days, {int(hours)} hours, {int(minutes)} minutes and {int(seconds)} seconds"

            embed = discord.Embed(
                title="Flooder Statistics",
                description=(
                    f"**Connected Bots `:`** `{len(self.bots)}`\n"
                    f"**Secured Users `:`** `{secured_users}`\n"
                    f"**Cpu Usage `:`** `{cpu_percent}`\n"
                    f"**Memory Usage `:`** `{memory:.2f} MB`\n"
                    f"**Ping `:`** `{ping}`ms\n"
                    f"**Uptime `:`** ```{formatted_uptime}```\n"
                    f"**Total Dm's Sent `:`** `{self.total_dms}`\n"
                    f"**DMs/Second `:`** `{self.total_dms / max(1, (datetime.datetime.now() - bot.start_time).total_seconds()):.2f}`"
                ),
                color=discord.Color.blue()
            )
        
            await ctx.send(embed=embed)

        @bot.command(name="premium")
        async def premium_command(ctx, action: str = None, user: discord.User = None):
            if bot.user.id != MAINIDD:
                return

            if ctx.author.id not in OWNER_IDS:
                embed = discord.Embed(
                    description="Only owners can manage premium users!",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return

            if action is None or action.lower() not in ("add", "rem", "list"):
                embed = discord.Embed(
                    description="Usage: `.premium add/rem/list (@user)`",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return

            premium_users = await self.load_premium_users()

            if action.lower() == "list":
                if not premium_users:
                    embed = discord.Embed(
                        description="No premium users.",
                        color=discord.Color.blue()
                    )
                else:
                    user_mentions = "\n".join(f"<@{uid}>" for uid in premium_users)
                    embed = discord.Embed(
                        title="Premium Users",
                        description=user_mentions,
                        color=discord.Color.gold()
                    )
                await ctx.send(embed=embed)
                return

            if user is None:
                embed = discord.Embed(
                    description=f"Usage: `.premium {action} @user`",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return

            if action.lower() == "add":
                if user.id in premium_users:
                    embed = discord.Embed(
                        description=f"{user.mention} is already a premium user!",
                        color=discord.Color.orange()
                    )
                else:
                    premium_users.add(user.id)
                    await self.save_premium_users(premium_users)
                    embed = discord.Embed(
                        description=f"{user.mention} has been added to premium!",
                        color=discord.Color.green()
                    )
            elif action.lower() == "rem":
                if user.id not in premium_users:
                    embed = discord.Embed(
                        description=f"{user.mention} is not a premium user!",
                        color=discord.Color.orange()
                    )
                else:
                    premium_users.discard(user.id)
                    await self.save_premium_users(premium_users)
                    embed = discord.Embed(
                        description=f"{user.mention} has been removed from premium!",
                        color=discord.Color.green()
                    )

            await ctx.send(embed=embed)

        @bot.command(name="unsecure")
        async def unsecure(ctx, user: discord.User = None):
            print("hii")
            if bot.user.id != 1413352398922973235:
                return

            if user is None:
                user = ctx.author

            secure_file = "secure.json"

            if os.path.exists(secure_file):
                with open(secure_file, "r") as file:
                    secure_users = set(json.load(file))
            else:
                secure_users = set()

            if ctx.author.id not in OWNER_IDS and user.id != ctx.author.id:
                embed = discord.Embed(
                    description="You can only unsecure yourself unless you're an owner.",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return

            if user.id not in secure_users:
                embed = discord.Embed(
                    description=f"{user.mention} is not secured!",
                    color=discord.Color.orange()
                )
                await ctx.send(embed=embed)
                return

            secure_users.remove(user.id)
            with open(secure_file, "w") as file:
                json.dump(list(secure_users), file)

            embed = discord.Embed(
                description=f"{user.mention} is no longer secured from flooding!",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)


        @bot.command(name="secure")
        async def secure(ctx, user: discord.User = None):
            print("hii")
            if bot.user.id != 1413352398922973235:
                return

            if user is None:
                user = ctx.author

            secure_file = "secure.json"

            if os.path.exists(secure_file):
                with open(secure_file, "r") as file:
                    secure_users = set(json.load(file))
            else:
                secure_users = set()

            if user.id in secure_users:
                embed = discord.Embed(
                    description=f"{user.mention} is already secured!",
                    color=discord.Color.orange()
                )
                await ctx.send(embed=embed)
                return

            if ctx.author.id not in OWNER_IDS and user.id != ctx.author.id:
                embed = discord.Embed(
                    description="You can only secure yourself unless you're an owner.",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return

            secure_users.add(user.id)
            with open(secure_file, "w") as file:
                json.dump(list(secure_users), file)

            embed = discord.Embed(
                description=f"{user.mention} is now secured from flooding!",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)


        @bot.command(name="help")
        async def help_command(ctx):
            if bot.user.id != MAINIDD:
                return

            help_embed = discord.Embed(title="Bot Commands", description="Here is a list of available commands:", color=discord.Color.blue())
            
            help_embed.add_field(name=".secure", value="Secures you from DM flooding.", inline=False)
            help_embed.add_field(name=".unsecure", value="Unsecures yourself or another user (only for owners).", inline=False)
            help_embed.add_field(name=".flood @user (reason)", value="Floods the target user's DMs with the given reason.", inline=False)
            help_embed.add_field(name=".stats", value="Shows bot statistics like active bots and secured users.", inline=False)
            help_embed.add_field(name=".premium add/rem/list (@user)", value="Manage premium users (owners only). Premium users can flood secured users.", inline=False)
            

            await ctx.send(embed=help_embed)



        @bot.command(name="flood")
        async def flood_command(ctx, user: discord.User = None, *, reason: str = "No reason provided"):
            if bot.user.id != MAINIDD:
                return

            if user is None:
                embed = discord.Embed(
                    description="Usage: `.flood @user (reason)`",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return

            # Check if requester is owner or premium (bypasses secure)
            premium_users = await self.load_premium_users()
            is_privileged = ctx.author.id in OWNER_IDS or ctx.author.id in premium_users

            try:
                with open("secure.json", "r") as file:
                    secure_users = json.load(file)
            except FileNotFoundError:
                secure_users = []

            if user.id in secure_users and not is_privileged:
                embed = discord.Embed(
                    description=f"{user.mention} is secured. You can't flood their DMs!",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return

            if user.bot:
                embed = discord.Embed(
                    description="Cannot flood bot accounts!",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return

            # Start extra bots for this flood operation
            extra_bots, extra_tasks = await self.start_extra_bots()
            all_flood_bots = self.bots[:]

            self.start_time = datetime.datetime.now()
            self.total_dms = 0

            embed = discord.Embed(
                title="Starting DM Flood",
                description=f"Target: {user.mention}\nReason: {reason}\nTotal Bots: {len(all_flood_bots)}",
                color=discord.Color.blue()
            )
            status_msg = await ctx.send(embed=embed)

            flood_message = discord.Embed(
                description=(
                    f"> You've been flooded by `{ctx.author.name}`!\n"
                    f"> Reason: `{reason}`\n"
                    f"> Want to avoid floods? Join our server and use .secure\n"
                ),
                color=discord.Color.blue()
            )

            flood_done_event = asyncio.Event()
            flood_tasks_done = 0
            total_flood_bots = len(all_flood_bots)

            async def update_status():
                while not flood_done_event.is_set():
                    duration = (datetime.datetime.now() - self.start_time).total_seconds()
                    dms_per_second = self.total_dms / duration if duration > 0 else 0
                    status_embed = discord.Embed(
                        title="Flood Status",
                        description=(
                            f"Target `:` {user.mention}\n"
                            f"Reason `:` `{reason}`\n"
                            f"Total DMs Sent `:` `{self.total_dms}`\n"
                            f"DMs per Second `:` `{dms_per_second:.2f}`\n"
                            f"Duration `:` `{duration:.1f}s`\n"
                            f"Active Bots `:` `{len(all_flood_bots)}`"
                        ),
                        color=discord.Color.blue()
                    )
                    await asyncio.sleep(3)
                    await status_msg.edit(embed=status_embed)

                duration = (datetime.datetime.now() - self.start_time).total_seconds()
                dms_per_second = self.total_dms / duration if duration > 0 else 0
                final_embed = discord.Embed(
                    title="Flood Completed",
                    description=(
                        f"Successfully sent `{self.total_dms}` DMs to {user.mention}.\n"
                        f"Duration: `{duration:.1f}s`\n"
                        f"DMs per Second: `{dms_per_second:.2f}`\n"
                        f"Reason: `{reason}`"
                    ),
                    color=discord.Color.green()
                )
                await status_msg.edit(embed=final_embed)

            async def send_dms_from_bot(flood_bot):
                nonlocal flood_tasks_done
                try:
                    target_user = await flood_bot.fetch_user(user.id)
                    dm_count = 0
                    while dm_count < MAX_DMS_PER_BOT:
                        try:
                            await target_user.send(embed=flood_message)
                            await flood_bot.increment_dm_count()
                            self.total_dms += 1
                            dm_count += 1
                        except discord.Forbidden:
                            break
                        except Exception as e:
                            logger.error(f"DM Error: {str(e)}")
                            break
                        await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"DM Thread Error: {str(e)}")
                finally:
                    flood_tasks_done += 1
                    if flood_tasks_done >= total_flood_bots:
                        flood_done_event.set()

            asyncio.create_task(update_status())
            for flood_bot in all_flood_bots:
                asyncio.create_task(send_dms_from_bot(flood_bot))

            # If somehow no bots are available, mark flood as done immediately
            if total_flood_bots == 0:
                flood_done_event.set()

            # Wait for all DM tasks to finish, then shut down extra bots
            await flood_done_event.wait()
            await self.stop_extra_bots(extra_bots, extra_tasks)

        


        @bot.event
        async def on_command_error(ctx, error):
            if bot.user.id != MAINIDD:
                return
            if isinstance(error, commands.CommandOnCooldown):
                embed = discord.Embed(description=f"Cooldown! Try again in {error.retry_after:.2f}s")
                await ctx.send(embed=embed)
                logger.warning(f"Rate-limited: {error.retry_after:.2f}s cooldown.")

    async def start_bot(self, token: str) -> bool:
        try:
            bot = FloodBot(token)
            await self.setup_bot_commands(bot)
            self.bots.append(bot)
            if not self.main_bot:
                self.main_bot = bot
            await bot.start(token)
            return True
        except Exception as e:
            logger.error(f"Failed to start bot: {str(e)}")
            return False

    async def start_main_bot_only(self):
        """Start only the main bot. Extra bots are started on demand during flood."""
        if not self.main_token:
            logger.error("Main bot token not found!")
            return

        logger.info("Starting main bot only (extra bots will start on .flood)...")
        await self.start_bot(self.main_token)

    async def cleanup(self):
        for bot in self.bots:
            try:
                await bot.close()
            except:
                pass

async def main():
    manager = BotManager()
    try:
        if await manager.load_tokens():
            await manager.start_main_bot_only()
        else:
            logger.error("Failed to load any valid tokens. Exiting...")
    except KeyboardInterrupt:
        logger.info("\nShutting down bots...")
        await manager.cleanup()
    except Exception as e:
        logger.critical(f"Fatal error: {str(e)}")
        await manager.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nBot shutdown complete")
    except Exception as e:
        logger.critical(f"Fatal error: {str(e)}")
