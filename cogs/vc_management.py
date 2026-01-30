"""
VC管理Cog

VC接続時間の管理と除外設定を提供します。
"""

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from datetime import datetime, timedelta

from config import DB_PATH, TZ
from database import fetch_one, fetch_all, upsert_user
from embeds import create_success_embed, create_error_embed, create_info_embed


class VCManagementCog(commands.Cog):
    """VC管理コマンド群"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    @app_commands.command(name="check", description="ユーザーのVC時間を確認する（管理者のみ）")
    @app_commands.describe(
        user="対象ユーザー",
        days="過去何日分（既定=7日、最大90日）"
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def check(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        days: int = 7
    ):
        """ユーザーのVC時間を確認"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        if days < 1 or days > 90:
            embed = create_error_embed("入力エラー", "日数は1〜90の範囲で指定してください。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            uid = await upsert_user(db, user.id)
            
            # 指定期間のVC時間を取得（joined_atではなくstart_timeを使用）
            start_date = (datetime.now(TZ) - timedelta(days=days)).isoformat()
            
            rows = await fetch_all(db, """
                SELECT 
                    DATE(start_time) as date,
                    SUM(duration_minutes) as total_minutes
                FROM vc_sessions
                WHERE user_id = ? AND guild_id = ? AND start_time >= ?
                GROUP BY DATE(start_time)
                ORDER BY date DESC
            """, (uid, str(interaction.guild.id), start_date))
            
            if not rows:
                embed = create_info_embed(
                    "VC時間確認",
                    f"**{user.display_name}** の過去{days}日間のVC接続記録はありません。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # 合計時間を計算
            total_minutes = sum(row[1] or 0 for row in rows)
            total_hours = total_minutes / 60
            
            embed = create_info_embed(
                "VC時間確認",
                f"**{user.display_name}** の過去{days}日間のVC接続時間",
                interaction.user
            )
            
            # 日別の詳細を追加（最新10日分）
            for date, minutes in rows[:10]:
                hours = minutes / 60
                embed.add_field(
                    name=f"📅 {date}",
                    value=f"⏱️ {hours:.2f}時間 ({minutes}分)",
                    inline=True
                )
            
            if len(rows) > 10:
                embed.add_field(
                    name="...",
                    value=f"他{len(rows) - 10}日分",
                    inline=False
                )
            
            embed.add_field(
                name="📊 合計",
                value=f"**{total_hours:.2f}時間** ({total_minutes}分)",
                inline=False
            )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="exclude_add", description="VC時間計測から除外するチャンネルを追加する（管理者のみ）")
    @app_commands.describe(channel="除外するチャンネル")
    @app_commands.default_permissions(manage_guild=True)
    async def exclude_add(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | discord.CategoryChannel
    ):
        """VC時間計測から除外するチャンネルを追加"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        channel_type = "category" if isinstance(channel, discord.CategoryChannel) else "voice"
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 既に除外設定されているかチェック
            existing = await fetch_one(db, """
                SELECT id FROM vc_excluded_channels
                WHERE guild_id = ? AND channel_id = ?
            """, (str(interaction.guild.id), str(channel.id)))
            
            if existing:
                embed = create_error_embed(
                    "設定エラー",
                    f"**{channel.name}** は既に除外設定されています。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # 除外設定を追加
            await db.execute("""
                INSERT INTO vc_excluded_channels(guild_id, channel_id, channel_type)
                VALUES (?, ?, ?)
            """, (str(interaction.guild.id), str(channel.id), channel_type))
            await db.commit()
        
        embed = create_success_embed(
            "除外設定追加",
            f"**{channel.name}** をVC時間計測から除外しました。\n\n"
            f"種類: {channel_type}\n"
            f"このチャンネル{'とその配下のチャンネル' if channel_type == 'category' else ''}での接続時間は記録されません。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)
    
    @app_commands.command(name="exclude_list", description="除外設定されたチャンネル一覧を表示する（管理者のみ）")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def exclude_list(self, interaction: discord.Interaction):
        """除外設定されたチャンネル一覧を表示"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await fetch_all(db, """
                SELECT channel_id, channel_type
                FROM vc_excluded_channels
                WHERE guild_id = ?
                ORDER BY channel_type, channel_id
            """, (str(interaction.guild.id),))
        
        if not rows:
            embed = create_info_embed(
                "除外チャンネル一覧",
                "VC時間計測から除外されているチャンネルはありません。",
                interaction.user
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        embed = create_info_embed(
            "除外チャンネル一覧",
            f"VC時間計測から除外されているチャンネル（{len(rows)}件）",
            interaction.user
        )
        
        categories = []
        voices = []
        
        for channel_id, channel_type in rows:
            channel = interaction.guild.get_channel(int(channel_id))
            if channel:
                if channel_type == "category":
                    categories.append(f"📁 {channel.name}")
                else:
                    voices.append(f"🔊 {channel.name}")
            else:
                if channel_type == "category":
                    categories.append(f"📁 (削除済み: {channel_id})")
                else:
                    voices.append(f"🔊 (削除済み: {channel_id})")
        
        if categories:
            embed.add_field(
                name="カテゴリー",
                value="\n".join(categories),
                inline=False
            )
        
        if voices:
            embed.add_field(
                name="ボイスチャンネル",
                value="\n".join(voices),
                inline=False
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="exclude_remove", description="チャンネルの除外設定を削除する（管理者のみ）")
    @app_commands.describe(channel="除外設定を解除するチャンネル")
    @app_commands.default_permissions(manage_guild=True)
    async def exclude_remove(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | discord.CategoryChannel
    ):
        """チャンネルの除外設定を削除"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 除外設定を削除
            cursor = await db.execute("""
                DELETE FROM vc_excluded_channels
                WHERE guild_id = ? AND channel_id = ?
            """, (str(interaction.guild.id), str(channel.id)))
            await db.commit()
            
            if cursor.rowcount == 0:
                embed = create_error_embed(
                    "設定エラー",
                    f"**{channel.name}** は除外設定されていません。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        embed = create_success_embed(
            "除外設定削除",
            f"**{channel.name}** の除外設定を削除しました。\n\n"
            f"このチャンネルでの接続時間が記録されるようになります。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)
    
    @app_commands.command(name="check_role_add", description="VC時間確認権限を持つロールを追加する（管理者のみ）")
    @app_commands.describe(role="権限を付与するロール")
    @app_commands.default_permissions(manage_guild=True)
    async def check_role_add(self, interaction: discord.Interaction, role: discord.Role):
        """VC時間確認権限を持つロールを追加"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 既に設定されているかチェック
            existing = await fetch_one(db, """
                SELECT id FROM vc_check_roles
                WHERE guild_id = ? AND role_id = ?
            """, (str(interaction.guild.id), str(role.id)))
            
            if existing:
                embed = create_error_embed(
                    "設定エラー",
                    f"**{role.name}** は既にVC時間確認権限を持っています。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # 権限を追加
            await db.execute("""
                INSERT INTO vc_check_roles(guild_id, role_id)
                VALUES (?, ?)
            """, (str(interaction.guild.id), str(role.id)))
            await db.commit()
        
        embed = create_success_embed(
            "権限追加",
            f"**{role.name}** にVC時間確認権限を付与しました。\n\n"
            f"このロールを持つユーザーは `/vc_check` コマンドで他のユーザーのVC時間を確認できます。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)
    
    @app_commands.command(name="check_role_list", description="VC時間確認権限を持つロール一覧を表示する（管理者のみ）")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def check_role_list(self, interaction: discord.Interaction):
        """VC時間確認権限を持つロール一覧を表示"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await fetch_all(db, """
                SELECT role_id
                FROM vc_check_roles
                WHERE guild_id = ?
                ORDER BY role_id
            """, (str(interaction.guild.id),))
        
        if not rows:
            embed = create_info_embed(
                "確認権限ロール一覧",
                "VC時間確認権限を持つロールはありません。",
                interaction.user
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        embed = create_info_embed(
            "確認権限ロール一覧",
            f"VC時間確認権限を持つロール（{len(rows)}件）",
            interaction.user
        )
        
        roles_list = []
        for (role_id,) in rows:
            role = interaction.guild.get_role(int(role_id))
            if role:
                roles_list.append(f"• {role.mention}")
            else:
                roles_list.append(f"• (削除済み: {role_id})")
        
        embed.add_field(
            name="ロール",
            value="\n".join(roles_list) if roles_list else "なし",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="check_role_remove", description="ロールのVC時間確認権限を削除する（管理者のみ）")
    @app_commands.describe(role="権限を削除するロール")
    @app_commands.default_permissions(manage_guild=True)
    async def check_role_remove(self, interaction: discord.Interaction, role: discord.Role):
        """ロールのVC時間確認権限を削除"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 権限を削除
            cursor = await db.execute("""
                DELETE FROM vc_check_roles
                WHERE guild_id = ? AND role_id = ?
            """, (str(interaction.guild.id), str(role.id)))
            await db.commit()
            
            if cursor.rowcount == 0:
                embed = create_error_embed(
                    "設定エラー",
                    f"**{role.name}** はVC時間確認権限を持っていません。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        embed = create_success_embed(
            "権限削除",
            f"**{role.name}** のVC時間確認権限を削除しました。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    """Cogをセットアップ"""
    await bot.add_cog(VCManagementCog(bot))
