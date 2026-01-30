"""
チャンネル管理Cog

チャンネルのメッセージを一括削除する機能を提供します。
"""

import discord
from discord import app_commands
from discord.ext import commands
import asyncio

from embeds import create_success_embed, create_error_embed


class ChannelManagementCog(commands.Cog):
    """チャンネル管理コマンド群"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    @app_commands.command(name="nuke", description="このチャンネルの全メッセージを削除（管理者のみ）")
    @app_commands.default_permissions(administrator=True)
    async def clear_channel(self, interaction: discord.Interaction):
        """チャンネルの全メッセージを削除"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
            embed = create_error_embed("実行エラー", "このコマンドはテキストチャンネルまたはVCチャンネルでのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # 即座に処理を開始（ephemeralで応答）
        await interaction.response.defer(ephemeral=True)
        
        # 進捗メッセージを送信
        progress_msg = await channel.send("🗑️ メッセージを取得中...")
        
        deleted_count = 0
        
        try:
            # まず全メッセージを取得
            all_messages = []
            async for msg in channel.history(limit=None):
                # 進捗メッセージ自体は除外
                if msg.id != progress_msg.id:
                    all_messages.append(msg)
            
            total_messages = len(all_messages)
            await progress_msg.edit(content=f"🗑️ {total_messages}件のメッセージを削除中...")
            
            # bulk_deleteで一括削除（最大100件ずつ、14日以内のメッセージのみ）
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            two_weeks_ago = now - timedelta(days=14)
            
            # 14日以内のメッセージと古いメッセージを分ける
            recent_messages = [msg for msg in all_messages if msg.created_at > two_weeks_ago]
            old_messages = [msg for msg in all_messages if msg.created_at <= two_weeks_ago]
            
            # 14日以内のメッセージを一括削除（100件ずつ）
            for i in range(0, len(recent_messages), 100):
                batch = recent_messages[i:i+100]
                try:
                    await channel.delete_messages(batch)
                    deleted_count += len(batch)
                    await progress_msg.edit(content=f"🗑️ メッセージを削除中... ({deleted_count}/{total_messages})")
                    await asyncio.sleep(1)  # レート制限対策
                except Exception as e:
                    print(f"一括削除エラー: {e}")
                    # 一括削除に失敗した場合は個別削除
                    for msg in batch:
                        try:
                            await msg.delete()
                            deleted_count += 1
                            await asyncio.sleep(0.5)
                        except:
                            pass
            
            # 14日以上古いメッセージは個別削除
            for msg in old_messages:
                try:
                    await msg.delete()
                    deleted_count += 1
                    if deleted_count % 10 == 0:
                        await progress_msg.edit(content=f"🗑️ メッセージを削除中... ({deleted_count}/{total_messages})")
                    await asyncio.sleep(0.5)  # レート制限対策
                except:
                    pass
            
            # 進捗メッセージを削除
            await progress_msg.delete()
            
            # 完了メッセージを送信
            embed = create_success_embed(
                "チャンネルクリア完了",
                f"#{channel.name} のログを削除しました\n\n**削除件数:** {deleted_count}件",
                interaction.user
            )
            await channel.send(embed=embed)
            
            # ephemeralで完了通知
            await interaction.followup.send(
                f"✅ #{channel.name} のメッセージを{deleted_count}件削除しました。",
                ephemeral=True
            )
            
        except Exception as e:
            embed = create_error_embed(
                "削除エラー",
                f"メッセージの削除中にエラーが発生しました:\n```\n{str(e)}\n```",
                interaction.user
            )
            await channel.send(embed=embed)
            await interaction.followup.send(f"❌ エラーが発生しました: {str(e)}", ephemeral=True)


async def setup(bot: commands.Bot):
    """Cogをセットアップ"""
    await bot.add_cog(ChannelManagementCog(bot))
