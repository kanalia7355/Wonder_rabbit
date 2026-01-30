"""
フォーラム管理Cog

フォーラムチャンネルとロールの連動システムを提供します。
指定ロールを持つユーザーのスレッドを自動作成します。
"""

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import asyncio

from config import DB_PATH
from database import fetch_one, fetch_all
from embeds import create_success_embed, create_error_embed, create_info_embed


class ForumManagerCog(commands.Cog):
    """フォーラム管理コマンド群"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    # コマンドグループの作成
    forum_group = app_commands.Group(
        name="forum",
        description="フォーラム管理システム（管理者のみ）",
        default_permissions=discord.Permissions(manage_guild=True)
    )
    
    @forum_group.command(name="setup", description="フォーラムとロールを紐付ける（管理者のみ）")
    @app_commands.describe(
        forum_channel="フォーラムチャンネル",
        role="紐付けるロール",
        delete_old_posts="ロール削除時に投稿も削除するか（既定=False）"
    )
    async def setup(
        self,
        interaction: discord.Interaction,
        forum_channel: discord.ForumChannel,
        role: discord.Role,
        delete_old_posts: bool = False
    ):
        """フォーラムとロールを紐付ける"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # 処理中メッセージを送信
        await interaction.response.defer(ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 既に設定されているかチェック
            existing = await fetch_one(db, """
                SELECT id FROM forum_settings
                WHERE guild_id = ? AND forum_channel_id = ? AND role_id = ?
            """, (str(interaction.guild.id), str(forum_channel.id), str(role.id)))
            
            if existing:
                embed = create_error_embed(
                    "設定エラー",
                    f"**{forum_channel.name}** と **{role.name}** の紐付けは既に設定されています。",
                    interaction.user
                )
                return await interaction.followup.send(embed=embed, ephemeral=True)
            
            # 設定を追加
            await db.execute("""
                INSERT INTO forum_settings(guild_id, forum_channel_id, role_id, delete_old_posts)
                VALUES (?, ?, ?, ?)
            """, (str(interaction.guild.id), str(forum_channel.id), str(role.id), 1 if delete_old_posts else 0))
            await db.commit()
        
        # 既にロールを持っているユーザーのスレッドを作成
        created_count = 0
        members_with_role = [member for member in interaction.guild.members if role in member.roles and not member.bot]
        
        for member in members_with_role:
            try:
                # スレッドを作成
                thread = await forum_channel.create_thread(
                    name=member.display_name,
                    content=f"{member.mention}",
                    reason=f"フォーラム管理: {role.name}ロール保持者用スレッド"
                )
                created_count += 1
                print(f"[FORUM] Created thread for {member.display_name} in {forum_channel.name}")
                
                # レート制限を避けるため少し待機
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"[FORUM] Error creating thread for {member.display_name}: {e}")
        
        embed = create_success_embed(
            "フォーラム設定完了",
            f"**{forum_channel.name}** と **{role.name}** を紐付けました。\n\n"
            f"📋 **設定内容:**\n"
            f"• フォーラム: {forum_channel.mention}\n"
            f"• ロール: {role.mention}\n"
            f"• 投稿削除: {'有効' if delete_old_posts else '無効'}\n"
            f"• 作成されたスレッド: **{created_count}件**\n\n"
            f"このロールが付与されたユーザーのスレッドが自動的に作成されます。",
            interaction.user
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    @forum_group.command(name="list", description="フォーラム設定一覧を表示する")
    async def list(self, interaction: discord.Interaction):
        """フォーラム設定一覧を表示"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await fetch_all(db, """
                SELECT forum_channel_id, role_id, delete_old_posts
                FROM forum_settings
                WHERE guild_id = ?
                ORDER BY forum_channel_id
            """, (str(interaction.guild.id),))
        
        if not rows:
            embed = create_info_embed(
                "フォーラム設定一覧",
                "フォーラム管理の設定はありません。\n\n"
                "`/forum_manager setup` コマンドで設定を追加できます。",
                interaction.user
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        embed = create_info_embed(
            "フォーラム設定一覧",
            f"フォーラム管理の設定（{len(rows)}件）",
            interaction.user
        )
        
        for forum_channel_id, role_id, delete_old_posts in rows:
            forum_channel = interaction.guild.get_channel(int(forum_channel_id))
            role = interaction.guild.get_role(int(role_id))
            
            forum_name = forum_channel.name if forum_channel else f"(削除済み: {forum_channel_id})"
            role_name = role.mention if role else f"(削除済み: {role_id})"
            delete_status = "✅ 有効" if delete_old_posts else "❌ 無効"
            
            embed.add_field(
                name=f"📋 {forum_name}",
                value=f"ロール: {role_name}\n投稿削除: {delete_status}",
                inline=False
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @forum_group.command(name="remove", description="フォーラム設定を削除する（管理者のみ）")
    @app_commands.describe(
        forum_channel="フォーラムチャンネル",
        role="紐付けを解除するロール"
    )
    async def remove(
        self,
        interaction: discord.Interaction,
        forum_channel: discord.ForumChannel,
        role: discord.Role
    ):
        """フォーラム設定を削除"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 設定を削除
            cursor = await db.execute("""
                DELETE FROM forum_settings
                WHERE guild_id = ? AND forum_channel_id = ? AND role_id = ?
            """, (str(interaction.guild.id), str(forum_channel.id), str(role.id)))
            await db.commit()
            
            if cursor.rowcount == 0:
                embed = create_error_embed(
                    "設定エラー",
                    f"**{forum_channel.name}** と **{role.name}** の紐付けは設定されていません。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        embed = create_success_embed(
            "設定削除完了",
            f"**{forum_channel.name}** と **{role.name}** の紐付けを削除しました。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)
    
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """メンバー更新時の処理（ロール追加を検知）"""
        # ロールが追加されたかチェック
        added_roles = set(after.roles) - set(before.roles)
        if not added_roles:
            return
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 追加されたロールが設定されているフォーラムを取得
            for role in added_roles:
                forums = await fetch_all(db, """
                    SELECT forum_channel_id FROM forum_settings
                    WHERE guild_id = ? AND role_id = ?
                """, (str(after.guild.id), str(role.id)))
                
                if not forums:
                    continue
                
                # 各フォーラムでスレッドを作成
                for (forum_channel_id,) in forums:
                    forum_channel = after.guild.get_channel(int(forum_channel_id))
                    if not forum_channel or not isinstance(forum_channel, discord.ForumChannel):
                        continue
                    
                    try:
                        # スレッドを作成
                        thread = await forum_channel.create_thread(
                            name=after.display_name,
                            content=f"{after.mention}",
                            reason=f"フォーラム管理: {role.name}ロール付与"
                        )
                        print(f"[FORUM] Created thread for {after.display_name} in {forum_channel.name} (role added)")
                    except Exception as e:
                        print(f"[FORUM] Error creating thread for {after.display_name}: {e}")


async def setup(bot: commands.Bot):
    """Cogをセットアップ"""
    await bot.add_cog(ForumManagerCog(bot))
