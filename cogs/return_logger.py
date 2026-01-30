"""
Return Logger Cog

Detects and notifies when users rejoin the server.
"""

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from datetime import datetime
import logging

from config import DB_PATH, TZ
from database import fetch_one, fetch_all, upsert_user
from embeds import create_success_embed, create_error_embed, create_info_embed

logger = logging.getLogger(__name__)


class ReturnLoggerCog(commands.Cog):
    """Return logger commands"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Handle member join event"""
        # Ignore bots
        if member.bot:
            return
        
        guild_id = str(member.guild.id)
        
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Get settings
                settings = await fetch_one(db, """
                    SELECT log_channel_id, enabled, notify_threshold
                    FROM return_logger_settings
                    WHERE guild_id = ?
                """, (guild_id,))
                
                # Skip if not configured or disabled
                if not settings or not settings[1]:
                    return
                
                log_channel_id, enabled, notify_threshold = settings
                
                # Register user
                uid = await upsert_user(db, member.id)
                
                # Get join history
                history = await fetch_one(db, """
                    SELECT join_count, first_joined_at
                    FROM user_join_history
                    WHERE guild_id = ? AND user_id = ?
                """, (guild_id, uid))
                
                now = datetime.now(TZ)
                
                if history:
                    # Returning user
                    join_count = history[0] + 1
                    first_joined = history[1]
                    
                    # Update history
                    await db.execute("""
                        UPDATE user_join_history
                        SET join_count = ?, last_joined_at = ?
                        WHERE guild_id = ? AND user_id = ?
                    """, (join_count, now.isoformat(), guild_id, uid))
                    
                    # Log the join
                    should_notify = join_count >= notify_threshold
                    await db.execute("""
                        INSERT INTO user_join_logs(guild_id, user_id, joined_at, join_number, notified)
                        VALUES (?, ?, ?, ?, ?)
                    """, (guild_id, uid, now.isoformat(), join_count, 1 if should_notify else 0))
                    
                    await db.commit()
                    
                    # Send notification
                    if should_notify and log_channel_id:
                        await self.send_return_notification(
                            member.guild, log_channel_id, member, join_count, first_joined
                        )
                        logger.info(f"[RETURN_LOGGER] Return detected: {member.name} ({join_count} times)")
                else:
                    # First join
                    await db.execute("""
                        INSERT INTO user_join_history(guild_id, user_id, join_count, first_joined_at, last_joined_at)
                        VALUES (?, ?, 1, ?, ?)
                    """, (guild_id, uid, now.isoformat(), now.isoformat()))
                    
                    # Log the join
                    await db.execute("""
                        INSERT INTO user_join_logs(guild_id, user_id, joined_at, join_number, notified)
                        VALUES (?, ?, ?, 1, 0)
                    """, (guild_id, uid, now.isoformat()))
                    
                    await db.commit()
                    logger.info(f"[RETURN_LOGGER] First join: {member.name}")
        except Exception as e:
            logger.error(f"[RETURN_LOGGER] Error: {e}")
    
    async def send_return_notification(
        self,
        guild: discord.Guild,
        log_channel_id: str,
        member: discord.Member,
        join_count: int,
        first_joined: str
    ):
        """Send return notification"""
        try:
            channel = guild.get_channel(int(log_channel_id))
            if not channel:
                logger.warning(f"[RETURN_LOGGER] Log channel not found: {log_channel_id}")
                return
            
            # Calculate days since first join
            first_date = datetime.fromisoformat(first_joined)
            days_since_first = (datetime.now(TZ) - first_date).days
            
            embed = discord.Embed(
                title="📥 再参加検知",
                description=f"{member.mention} がサーバーに再参加しました",
                color=discord.Color.orange()
            )
            embed.add_field(name="参加回数", value=f"{join_count} 回", inline=True)
            embed.add_field(name="初回参加", value=f"{days_since_first} 日前", inline=True)
            embed.add_field(name="ユーザーID", value=f"`{member.id}`", inline=False)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=f"アカウント作成日: {member.created_at.strftime('%Y-%m-%d')}")
            
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"[RETURN_LOGGER] Notification error: {e}")
    
    # Command group
    return_logger_group = app_commands.Group(
        name="return_logger",
        description="再参加検知ロガーの管理（管理者のみ）",
        default_permissions=discord.Permissions(administrator=True)
    )
    
    @return_logger_group.command(name="setup", description="再参加ロガーを設定（管理者のみ）")
    @app_commands.describe(
        log_channel="通知用ログチャンネル",
        notify_threshold="N回目の参加から通知（デフォルト: 2）"
    )
    async def setup_logger(
        self,
        interaction: discord.Interaction,
        log_channel: discord.TextChannel,
        notify_threshold: int = 2
    ):
        """Configure return logger"""
        if not interaction.guild:
            embed = create_error_embed("Execution Error", "This command can only be used in a server.", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        if notify_threshold < 1:
            embed = create_error_embed("入力エラー", "通知しきい値は1以上である必要があります。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        guild_id = str(interaction.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Save settings
            await db.execute("""
                INSERT INTO return_logger_settings(guild_id, log_channel_id, notify_threshold)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    log_channel_id = excluded.log_channel_id,
                    notify_threshold = excluded.notify_threshold,
                    updated_at = CURRENT_TIMESTAMP
            """, (guild_id, str(log_channel.id), notify_threshold))
            await db.commit()
        
        embed = create_success_embed(
            "再参加ロガー設定完了",
            f"**ログチャンネル:** {log_channel.mention}\n"
            f"**通知しきい値:** {notify_threshold}回目の参加から\n\n"
            f"再参加ロガーが有効化されました。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @return_logger_group.command(name="stats", description="再参加統計を表示（管理者のみ）")
    async def show_stats(self, interaction: discord.Interaction):
        """Show return statistics"""
        if not interaction.guild:
            embed = create_error_embed("Execution Error", "This command can only be used in a server.", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        guild_id = str(interaction.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Total users
            total_users = await fetch_one(db, """
                SELECT COUNT(*) FROM user_join_history WHERE guild_id = ?
            """, (guild_id,))
            
            # Return users (2+ joins)
            return_users = await fetch_one(db, """
                SELECT COUNT(*) FROM user_join_history WHERE guild_id = ? AND join_count >= 2
            """, (guild_id,))
            
            # Average joins
            avg_joins = await fetch_one(db, """
                SELECT AVG(join_count) FROM user_join_history WHERE guild_id = ?
            """, (guild_id,))
            
            # Top user
            top_user = await fetch_one(db, """
                SELECT user_id, join_count FROM user_join_history 
                WHERE guild_id = ? 
                ORDER BY join_count DESC 
                LIMIT 1
            """, (guild_id,))
            
            if not total_users or total_users[0] == 0:
                embed = create_info_embed(
                    "Return Statistics",
                    "No data available yet.",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            embed = discord.Embed(
                title="📊 再参加統計",
                color=discord.Color.blue()
            )
            embed.add_field(name="総ユーザー数", value=f"{total_users[0]} 人", inline=True)
            embed.add_field(name="再参加ユーザー", value=f"{return_users[0]} 人", inline=True)
            embed.add_field(name="平均参加回数", value=f"{avg_joins[0]:.2f} 回", inline=True)
            
            if top_user and top_user[0]:
                # Get user info
                user_row = await fetch_one(db, "SELECT discord_user_id FROM users WHERE id = ?", (top_user[0],))
                if user_row:
                    try:
                        top_member = await interaction.guild.fetch_member(int(user_row[0]))
                        embed.add_field(
                            name="Top User",
                            value=f"{top_member.mention} ({top_user[1]} times)",
                            inline=False
                        )
                    except:
                        embed.add_field(
                            name="トップユーザー",
                            value=f"ID: {user_row[0]} ({top_user[1]} 回)",
                            inline=False
                        )
            
            embed.set_footer(text=f"Requested by: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @return_logger_group.command(name="history", description="ユーザーの参加履歴を表示（管理者のみ）")
    @app_commands.describe(user="ユーザー")
    async def show_history(
        self,
        interaction: discord.Interaction,
        user: discord.Member
    ):
        """Show user's join history"""
        if not interaction.guild:
            embed = create_error_embed("Execution Error", "This command can only be used in a server.", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        guild_id = str(interaction.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Get user ID
            user_row = await fetch_one(db, "SELECT id FROM users WHERE discord_user_id = ?", (str(user.id),))
            if not user_row:
                embed = create_info_embed(
                    "Join History",
                    f"{user.mention} has no join history.",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            uid = user_row[0]
            
            # Get join history
            history = await fetch_one(db, """
                SELECT join_count, first_joined_at, last_joined_at
                FROM user_join_history
                WHERE guild_id = ? AND user_id = ?
            """, (guild_id, uid))
            
            if not history:
                embed = create_info_embed(
                    "Join History",
                    f"{user.mention} has no join history.",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            join_count, first_joined, last_joined = history
            
            # Get recent logs
            recent_logs = await fetch_all(db, """
                SELECT joined_at, join_number, notified
                FROM user_join_logs
                WHERE guild_id = ? AND user_id = ?
                ORDER BY joined_at DESC
                LIMIT 5
            """, (guild_id, uid))
            
            embed = discord.Embed(
                title=f"📋 {user.display_name}'s Join History",
                color=discord.Color.blue()
            )
            embed.set_thumbnail(url=user.display_avatar.url)
            embed.add_field(name="Total Joins", value=f"{join_count} times", inline=True)
            embed.add_field(name="First Join", value=f"<t:{int(datetime.fromisoformat(first_joined).timestamp())}:R>", inline=True)
            embed.add_field(name="Last Join", value=f"<t:{int(datetime.fromisoformat(last_joined).timestamp())}:R>", inline=True)
            
            if recent_logs:
                log_text = []
                for joined_at, join_number, notified in recent_logs:
                    timestamp = int(datetime.fromisoformat(joined_at).timestamp())
                    notify_mark = "🔔" if notified else ""
                    log_text.append(f"{notify_mark} {join_number} time(s): <t:{timestamp}:R>")
                
                embed.add_field(
                    name="Recent Joins",
                    value="\n".join(log_text),
                    inline=False
                )
            
            embed.set_footer(text=f"Requested by: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @return_logger_group.command(name="enable", description="再参加ロガーを有効化（管理者のみ）")
    async def enable_logger(self, interaction: discord.Interaction):
        """Enable return logger"""
        if not interaction.guild:
            embed = create_error_embed("Execution Error", "This command can only be used in a server.", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        guild_id = str(interaction.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Check if settings exist
            settings = await fetch_one(db, """
                SELECT log_channel_id FROM return_logger_settings WHERE guild_id = ?
            """, (guild_id,))
            
            if not settings or not settings[0]:
                embed = create_error_embed(
                    "Configuration Error",
                    "Please configure the log channel first using `/return_logger setup`.",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # Enable
            await db.execute("""
                UPDATE return_logger_settings SET enabled = 1, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ?
            """, (guild_id,))
            await db.commit()
        
        embed = create_success_embed(
            "再参加ロガー有効化",
            "再参加ロガーが有効化されました。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @return_logger_group.command(name="disable", description="再参加ロガーを無効化（管理者のみ）")
    async def disable_logger(self, interaction: discord.Interaction):
        """Disable return logger"""
        if not interaction.guild:
            embed = create_error_embed("Execution Error", "This command can only be used in a server.", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        guild_id = str(interaction.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE return_logger_settings SET enabled = 0, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ?
            """, (guild_id,))
            await db.commit()
        
        embed = create_success_embed(
            "再参加ロガー無効化",
            "再参加ロガーが無効化されました。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @return_logger_group.command(name="initialize", description="現在のメンバーを初回参加者として登録（管理者のみ）")
    async def initialize_members(self, interaction: discord.Interaction):
        """Register current members as first-time joiners"""
        if not interaction.guild:
            embed = create_error_embed("Execution Error", "This command can only be used in a server.", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        
        guild_id = str(interaction.guild.id)
        now = datetime.now(TZ)
        
        registered_count = 0
        skipped_count = 0
        
        async with aiosqlite.connect(DB_PATH) as db:
            for member in interaction.guild.members:
                # Ignore bots
                if member.bot:
                    continue
                
                # Register user
                uid = await upsert_user(db, member.id)
                
                # Check if already exists
                existing = await fetch_one(db, """
                    SELECT id FROM user_join_history
                    WHERE guild_id = ? AND user_id = ?
                """, (guild_id, uid))
                
                if existing:
                    skipped_count += 1
                    continue
                
                # Register as first join
                await db.execute("""
                    INSERT INTO user_join_history(guild_id, user_id, join_count, first_joined_at, last_joined_at)
                    VALUES (?, ?, 1, ?, ?)
                """, (guild_id, uid, now.isoformat(), now.isoformat()))
                
                # Log the join
                await db.execute("""
                    INSERT INTO user_join_logs(guild_id, user_id, joined_at, join_number, notified)
                    VALUES (?, ?, ?, 1, 0)
                """, (guild_id, uid, now.isoformat()))
                
                registered_count += 1
            
            await db.commit()
        
        embed = create_success_embed(
            "メンバー初期化完了",
            f"**登録済み:** {registered_count} 人\n"
            f"**スキップ:** {skipped_count} 人（既に登録済み）\n\n"
            f"現在のメンバーが初回参加者として登録されました。",
            interaction.user
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"[RETURN_LOGGER] Member initialization: {registered_count} registered, {skipped_count} skipped")


async def setup(bot: commands.Bot):
    """Setup cog"""
    await bot.add_cog(ReturnLoggerCog(bot))
