"""
送金ログCog

全てのトランザクションを指定チャンネルにログ送信する機能を提供します。
"""

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from decimal import Decimal
from datetime import datetime
import logging

from config import DB_PATH, TZ
from database import fetch_one
from embeds import create_success_embed, create_error_embed

logger = logging.getLogger(__name__)


async def send_transaction_log(
    bot, guild_id: str, tx_kind: str,
    from_user_id: int, to_user_id: int,
    amount: Decimal, currency: str, reference: str = None
):
    """送金ログを送信"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # 設定を取得
            settings = await fetch_one(db, """
                SELECT log_channel_id, enabled
                FROM transaction_log_settings
                WHERE guild_id = ?
            """, (guild_id,))
            
            if not settings or not settings[1]:  # enabled
                return
            
            log_channel_id = settings[0]
            if not log_channel_id:
                return
            
            guild = bot.get_guild(int(guild_id))
            if not guild:
                return
            
            channel = guild.get_channel(int(log_channel_id))
            if not channel:
                return
            
            # 送信者・受信者を取得
            from_user = guild.get_member(from_user_id) if from_user_id else None
            to_user = guild.get_member(to_user_id) if to_user_id else None
            
            # Embedを作成
            embed = discord.Embed(
                title="送金ログ",
                color=discord.Color.blue(),
                timestamp=datetime.now(TZ)
            )
            
            # トランザクション種別
            kind_names = {
                'transfer': '送金',
                'monthly_allowance': '月次給料',
                'auto_reward': '自動報酬',
                'role_purchase': 'ロール購入',
                'bet': '賭け',
                'bet_payout': '賭け配当',
                'bet_refund': '賭け返金'
            }
            kind_name = kind_names.get(tx_kind, tx_kind)
            
            embed.add_field(name="種別", value=kind_name, inline=True)
            embed.add_field(name="金額", value=f"{amount} {currency}", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)  # 空白
            
            # 送信者
            from_text = from_user.mention if from_user else "システム"
            embed.add_field(name="送信者", value=from_text, inline=True)
            
            # 受信者
            to_text = to_user.mention if to_user else "システム"
            embed.add_field(name="受信者", value=to_text, inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)  # 空白
            
            if reference:
                embed.add_field(name="備考", value=reference, inline=False)
            
            await channel.send(embed=embed)
            logger.info(f"[TX_LOG] Sent log: {kind_name} {amount} {currency}")
    except Exception as e:
        logger.error(f"[TX_LOG] Error sending log: {e}")


class TransactionLoggerCog(commands.Cog):
    """送金ログコマンド群"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    # コマンドグループ
    log_group = app_commands.Group(
        name="txlog",
        description="送金ログ管理（管理者のみ）",
        default_permissions=discord.Permissions(administrator=True)
    )
    
    @log_group.command(name="setup", description="送金ログチャンネルを設定（管理者のみ）")
    @app_commands.describe(channel="ログ送信先チャンネル")
    async def setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel
    ):
        """送金ログチャンネルを設定"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        guild_id = str(interaction.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 設定を保存
            await db.execute("""
                INSERT INTO transaction_log_settings(guild_id, log_channel_id, enabled)
                VALUES (?, ?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET
                    log_channel_id = excluded.log_channel_id,
                    enabled = 1,
                    updated_at = CURRENT_TIMESTAMP
            """, (guild_id, str(channel.id)))
            await db.commit()
        
        embed = create_success_embed(
            "送金ログ設定完了",
            f"**ログチャンネル:** {channel.mention}\n\n"
            f"全ての送金がこのチャンネルに記録されます。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @log_group.command(name="enable", description="送金ログを有効化（管理者のみ）")
    async def enable(self, interaction: discord.Interaction):
        """送金ログを有効化"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        guild_id = str(interaction.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 設定が存在するか確認
            settings = await fetch_one(db, """
                SELECT log_channel_id FROM transaction_log_settings WHERE guild_id = ?
            """, (guild_id,))
            
            if not settings or not settings[0]:
                embed = create_error_embed(
                    "設定エラー",
                    "先に `/txlog setup` でチャンネルを設定してください。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # 有効化
            await db.execute("""
                UPDATE transaction_log_settings SET enabled = 1, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ?
            """, (guild_id,))
            await db.commit()
        
        embed = create_success_embed(
            "送金ログ有効化",
            "送金ログを有効化しました。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @log_group.command(name="disable", description="送金ログを無効化（管理者のみ）")
    async def disable(self, interaction: discord.Interaction):
        """送金ログを無効化"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        guild_id = str(interaction.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE transaction_log_settings SET enabled = 0, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ?
            """, (guild_id,))
            await db.commit()
        
        embed = create_success_embed(
            "送金ログ無効化",
            "送金ログを無効化しました。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    """Cogをセットアップ"""
    await bot.add_cog(TransactionLoggerCog(bot))