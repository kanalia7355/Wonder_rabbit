"""
ロール期限管理Cog

期限付きロールの自動剥奪を管理します。
"""

import discord
from discord.ext import commands, tasks
import aiosqlite
from datetime import datetime

from config import DB_PATH, TZ
from database import fetch_all, fetch_one


class RoleExpiryCog(commands.Cog):
    """ロール期限管理コマンド群"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_expired_roles.start()
    
    def cog_unload(self):
        """Cogアンロード時にタスクを停止"""
        self.check_expired_roles.cancel()
    
    @tasks.loop(minutes=5)  # 5分ごとにチェック
    async def check_expired_roles(self):
        """期限切れのロールをチェックして剥奪"""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # 現在時刻を取得
                now = datetime.now(TZ)
                
                # 期限切れのロール購入を取得
                expired_purchases = await fetch_all(db, """
                    SELECT rp.id, rp.user_id, rp.plan_id, rp.guild_id, rp.expires_at, 
                           pl.role_id, u.discord_user_id
                    FROM role_purchases rp
                    JOIN role_plans pl ON rp.plan_id = pl.id
                    JOIN users u ON rp.user_id = u.id
                    WHERE datetime(rp.expires_at) <= datetime(?)
                """, (now.isoformat(),))
                
                for purchase_id, user_id, plan_id, guild_id, expires_at, role_id, discord_user_id in expired_purchases:
                    try:
                        # ギルドを取得
                        guild = self.bot.get_guild(int(guild_id))
                        if not guild:
                            print(f"[ROLE_EXPIRY] ギルド {guild_id} が見つかりません")
                            continue
                        
                        # メンバーを取得
                        member = guild.get_member(int(discord_user_id))
                        if not member:
                            print(f"[ROLE_EXPIRY] メンバー {discord_user_id} がギルド {guild_id} に見つかりません")
                            # 購入記録を削除
                            await db.execute("DELETE FROM role_purchases WHERE id = ?", (purchase_id,))
                            continue
                        
                        # ロールを取得
                        role = guild.get_role(int(role_id))
                        if not role:
                            print(f"[ROLE_EXPIRY] ロール {role_id} がギルド {guild_id} に見つかりません")
                            # 購入記録を削除
                            await db.execute("DELETE FROM role_purchases WHERE id = ?", (purchase_id,))
                            continue
                        
                        # ロールを剥奪
                        if role in member.roles:
                            await member.remove_roles(role)
                            print(f"[ROLE_EXPIRY] {member.display_name} から {role.name} を剥奪しました（期限切れ: {expires_at}）")
                        
                        # 購入記録を削除
                        await db.execute("DELETE FROM role_purchases WHERE id = ?", (purchase_id,))
                        
                    except Exception as e:
                        print(f"[ROLE_EXPIRY] ロール剥奪中にエラーが発生しました: {e}")
                        continue
                
                # 変更をコミット
                await db.commit()
                
                if expired_purchases:
                    print(f"[ROLE_EXPIRY] {len(expired_purchases)}件の期限切れロールを処理しました")
                    
        except Exception as e:
            print(f"[ROLE_EXPIRY] 期限切れロールのチェック中にエラーが発生しました: {e}")
    
    @check_expired_roles.before_loop
    async def before_check_expired_roles(self):
        """タスク開始前にBotの準備を待つ"""
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    """Cogをセットアップ"""
    await bot.add_cog(RoleExpiryCog(bot))
