"""
VC Earning System

Monitors user VC time and automatically awards currency every minute
based on per-category rates.
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
from decimal import Decimal
from datetime import datetime, timedelta
import logging
import asyncio

from config import DB_PATH, TZ
from database import fetch_one, fetch_all, get_asset, ensure_user_account, upsert_user, new_transaction, post_ledger
from utils import has_bank_permission

logger = logging.getLogger(__name__)


class VCEarningCog(commands.Cog):
    """VC earning functionality"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.payout_task.start()
        self.daily_reset_task.start()
    
    def cog_unload(self):
        """Stop tasks when cog unloads"""
        self.payout_task.cancel()
        self.daily_reset_task.cancel()
    
    @tasks.loop(hours=24)
    async def daily_reset_task(self):
        """Reset daily earnings at midnight"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Reset all daily earnings
                await db.execute("DELETE FROM vc_earning_daily WHERE date < date('now', '-7 days')")
                await db.commit()
                logger.info("[VC_EARNING] Daily reset completed - old records cleaned")
        except Exception as e:
            logger.error(f"[VC_EARNING] Daily reset error: {e}")
    
    @daily_reset_task.before_loop
    async def before_daily_reset_task(self):
        """Wait for bot ready and next midnight"""
        await self.bot.wait_until_ready()
        
        # Wait until next midnight
        now = datetime.now(TZ)
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait_seconds = (next_midnight - now).total_seconds()
        
        logger.info(f"[VC_EARNING] Daily reset task will start at {next_midnight} (in {wait_seconds/3600:.1f} hours)")
        await asyncio.sleep(wait_seconds)
    
    @tasks.loop(seconds=60)
    async def payout_task(self):
        """Award earnings every minute"""
        try:
            now = datetime.now(TZ)
            today = now.strftime("%Y-%m-%d")
            
            async with aiosqlite.connect(DB_PATH) as db:
                # Get active sessions
                sessions = await fetch_all(db, """
                    SELECT s.user_id, s.guild_id, s.channel_id, s.category_id, s.started_at
                    FROM vc_earning_sessions s
                """)
                
                for user_id, guild_id, channel_id, category_id, started_at_str in sessions:
                    # Get rate for this category
                    rate_info = await fetch_one(db, """
                        SELECT r.asset_id, r.rate_per_minute, a.symbol, a.decimals
                        FROM vc_earning_rates r
                        JOIN assets a ON r.asset_id = a.id
                        WHERE r.guild_id = ? AND r.category_id = ?
                    """, (str(guild_id), str(category_id)))
                    
                    if not rate_info:
                        logger.debug(f"[VC_EARNING] No rate configured for category {category_id} in guild {guild_id}")
                        continue
                    
                    asset_id, rate_str, symbol, decimals = rate_info
                    rate = Decimal(rate_str)
                    
                    # Verify user is still in VC
                    guild = self.bot.get_guild(int(guild_id))
                    if not guild:
                        logger.warning(f"[VC_EARNING] Guild {guild_id} not found")
                        await db.execute("DELETE FROM vc_earning_sessions WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
                        continue
                    
                    member = guild.get_member(int(user_id))
                    if not member or not member.voice or not member.voice.channel:
                        logger.warning(f"[VC_EARNING] User {user_id} not in VC, removing session")
                        await db.execute("DELETE FROM vc_earning_sessions WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
                        continue
                    
                    # Verify correct channel
                    if str(member.voice.channel.id) != str(channel_id):
                        logger.warning(f"[VC_EARNING] User {user_id} in different channel")
                        await db.execute("DELETE FROM vc_earning_sessions WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
                        continue
                    
                    # Calculate earnings (1 minute worth)
                    earnings = rate.quantize(Decimal(10) ** -int(decimals))
                    
                    if earnings <= 0:
                        continue
                    
                    # Get user account
                    uid = await upsert_user(db, user_id)
                    user_account_id = await ensure_user_account(db, user_id, guild_id)
                    
                    # Create transaction
                    tx_id = await new_transaction(
                        db,
                        kind='vc_earning',
                        created_by_user_id=None,
                        unique_hash=None,
                        reference=f'VC Earning: 1 min @ {rate} {symbol}/min'
                    )
                    
                    # Add to balance
                    await post_ledger(db, tx_id, user_account_id, asset_id, earnings)
                    
                    # Update daily earnings
                    await db.execute("""
                        INSERT INTO vc_earning_daily(guild_id, user_id, asset_id, total_earned, date)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id, user_id, asset_id, date) DO UPDATE SET
                            total_earned = total_earned + excluded.total_earned
                    """, (str(guild_id), uid, asset_id, str(earnings), today))
                    
                    logger.info(f"[VC_EARNING] Paid {earnings} {symbol} to user {user_id} in guild {guild_id}")
                
                await db.commit()
        
        except Exception as e:
            logger.error(f"[VC_EARNING] Payout task error: {e}")
    
    @payout_task.before_loop
    async def before_payout_task(self):
        """Wait for bot ready before starting payout task"""
        await self.bot.wait_until_ready()
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Initialize sessions for users in VCs on bot startup"""
        logger.info("[VC_EARNING] Initializing sessions for users in VCs...")
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Clear existing sessions (bot restart)
            await db.execute("DELETE FROM vc_earning_sessions")
            await db.commit()
            
            # Check all guilds
            for guild in self.bot.guilds:
                # Check all voice channels
                for channel in guild.voice_channels:
                    # Check members in channel
                    for member in channel.members:
                        if member.bot:
                            continue
                        
                        # Start session
                        await self._start_session(db, member, channel)
            
            await db.commit()
        
        logger.info("[VC_EARNING] Session initialization complete")
    
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Handle VC state changes"""
        if member.bot:
            return
        
        guild_id = member.guild.id
        user_id = member.id
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Left VC
            if before.channel and not after.channel:
                await self._end_session(db, user_id, guild_id)
            
            # Joined VC
            elif not before.channel and after.channel:
                await self._start_session(db, member, after.channel)
            
            # Moved between VCs
            elif before.channel and after.channel:
                before_category = before.channel.category_id if before.channel.category else None
                after_category = after.channel.category_id if after.channel.category else None
                
                # Category changed
                if before_category != after_category:
                    await self._end_session(db, user_id, guild_id)
                    await self._start_session(db, member, after.channel)
                else:
                    # Same category, just update channel ID
                    await db.execute("""
                        UPDATE vc_earning_sessions
                        SET channel_id = ?
                        WHERE user_id = ? AND guild_id = ?
                    """, (str(after.channel.id), user_id, str(guild_id)))
            
            await db.commit()
    
    async def _start_session(self, db: aiosqlite.Connection, member: discord.Member, channel: discord.VoiceChannel):
        """Start a VC earning session"""
        now = datetime.now(TZ)
        category_id = str(channel.category_id) if channel.category else None
        
        # Get user internal ID
        uid = await upsert_user(db, member.id)
        
        await db.execute("""
            INSERT OR REPLACE INTO vc_earning_sessions
            (guild_id, user_id, channel_id, category_id, started_at)
            VALUES (?, ?, ?, ?, ?)
        """, (str(member.guild.id), uid, str(channel.id), category_id, now.isoformat()))
        
        logger.info(f"[VC_EARNING] Started session for user {member.id} in channel {channel.id}")
    
    async def _end_session(self, db: aiosqlite.Connection, user_id: int, guild_id: int):
        """End a VC earning session"""
        # Get user internal ID
        uid = await upsert_user(db, user_id)
        
        # Delete session
        await db.execute("DELETE FROM vc_earning_sessions WHERE user_id = ? AND guild_id = ?", (uid, str(guild_id)))
        logger.info(f"[VC_EARNING] Ended session for user {user_id}")
    
    # Command group
    vc_earning_group = app_commands.Group(
        name="vc_earning",
        description="VC報酬システム"
    )
    
    @vc_earning_group.command(name="setup", description="VC報酬レートを設定（管理者のみ）")
    @app_commands.describe(
        category="設定するカテゴリ",
        currency_symbol="通貨シンボル",
        rate_per_minute="1分あたりのレート"
    )
    @app_commands.default_permissions(administrator=True)
    async def setup_earning(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        currency_symbol: str,
        rate_per_minute: float
    ):
        """Configure VC earning rate"""
        if not await has_bank_permission(interaction):
            await interaction.response.send_message(
                "❌ You don't have permission to use this feature.",
                ephemeral=True
            )
            return
        
        # Check if currency exists
        async with aiosqlite.connect(DB_PATH) as db:
            asset = await get_asset(db, currency_symbol.upper(), interaction.guild_id)
            if not asset:
                await interaction.response.send_message(
                    f"❌ Currency `{currency_symbol}` not found.",
                    ephemeral=True
                )
                return
            
            asset_id = asset[0]
            
            # Save rate
            await db.execute("""
                INSERT INTO vc_earning_rates(guild_id, category_id, asset_id, rate_per_minute)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, category_id) DO UPDATE SET
                    asset_id = excluded.asset_id,
                    rate_per_minute = excluded.rate_per_minute
            """, (str(interaction.guild_id), str(category.id), asset_id, str(rate_per_minute)))
            
            await db.commit()
        
        embed = discord.Embed(
            title="✅ VC報酬設定完了",
            color=0x2ecc71
        )
        embed.add_field(name="カテゴリ", value=category.name, inline=False)
        embed.add_field(name="通貨", value=currency_symbol.upper(), inline=True)
        embed.add_field(name="レート", value=f"{rate_per_minute} {currency_symbol.upper()}/分", inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @vc_earning_group.command(name="check", description="現在のVC報酬状況を確認")
    async def check_earning(self, interaction: discord.Interaction):
        """Check current VC earning status"""
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "❌ 現在ボイスチャンネルに接続していません。",
                ephemeral=True
            )
            return
        
        channel = interaction.user.voice.channel
        category = channel.category
        category_id = str(category.id) if category else None
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Get rate info
            rate_info = await fetch_one(db, """
                SELECT r.rate_per_minute, a.symbol
                FROM vc_earning_rates r
                JOIN assets a ON r.asset_id = a.id
                WHERE r.guild_id = ? AND r.category_id = ?
            """, (str(interaction.guild_id), category_id))
            
            if rate_info:
                rate, currency_symbol = rate_info
                
                # Get today's earnings
                today = datetime.now(TZ).strftime("%Y-%m-%d")
                uid = await upsert_user(db, interaction.user.id)
                
                daily_earnings = await fetch_one(db, """
                    SELECT total_earned FROM vc_earning_daily
                    WHERE guild_id = ? AND user_id = ? AND date = ?
                """, (str(interaction.guild_id), uid, today))
                
                total_earned = Decimal(daily_earnings[0]) if daily_earnings else Decimal("0")
                
                # Calculate connection time
                if Decimal(rate) > 0:
                    connection_minutes = int(total_earned / Decimal(rate))
                else:
                    connection_minutes = 0
            else:
                # No rate configured
                currency_symbol = "UNKNOWN"
                rate = 0
                connection_minutes = 0
                total_earned = 0
            
            # Display
            embed = discord.Embed(
                title="VC報酬状況",
                color=0x3498db
            )
            embed.add_field(
                name="---------------",
                value=(
                    f"**接続中**: {channel.name}\n"
                    f"**レート**: {int(float(rate))} {currency_symbol}/分\n"
                    "---------------"
                ),
                inline=False
            )
            embed.add_field(name="接続時間", value=f"{connection_minutes} 分", inline=True)
            embed.add_field(name="今日の獲得量", value=f"{int(float(total_earned))} {currency_symbol}", inline=True)
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @vc_earning_group.command(name="debug_sessions", description="アクティブなセッションを表示（管理者のみ）")
    @app_commands.default_permissions(administrator=True)
    async def debug_sessions(self, interaction: discord.Interaction):
        """Debug: Show all active sessions"""
        async with aiosqlite.connect(DB_PATH) as db:
            sessions = await fetch_all(db, """
                SELECT s.user_id, s.channel_id, s.category_id, s.started_at, u.discord_user_id
                FROM vc_earning_sessions s
                JOIN users u ON s.user_id = u.id
                WHERE s.guild_id = ?
            """, (str(interaction.guild_id),))
            
            if not sessions:
                await interaction.response.send_message("⚠️ No active sessions", ephemeral=True)
                return
            
            embed = discord.Embed(title="🔍 Active Sessions", color=0xe74c3c)
            
            for user_id, channel_id, category_id, started_at, discord_user_id in sessions:
                user = interaction.guild.get_member(int(discord_user_id))
                channel = interaction.guild.get_channel(int(channel_id))
                
                embed.add_field(
                    name=f"👤 {user.display_name if user else f'User {discord_user_id}'}",
                    value=(
                        f"**Channel**: {channel.name if channel else f'ID: {channel_id}'}\n"
                        f"**Category ID**: {category_id}\n"
                        f"**Started**: {started_at}"
                    ),
                    inline=False
                )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    """Setup the cog"""
    await bot.add_cog(VCEarningCog(bot))
