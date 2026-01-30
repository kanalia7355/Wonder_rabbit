"""
月次自動送金Cog

月末（毎月28日）にロール毎に設定した通貨を自動送金する機能を提供します。
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
from decimal import Decimal
from datetime import datetime
import logging

from config import DB_PATH, TZ
from database import (
    fetch_one, fetch_all, upsert_user, ensure_user_account,
    account_id_by_name, new_transaction, post_ledger,
    get_asset, auto_refill_treasury_if_needed, balance_of
)
from embeds import create_success_embed, create_error_embed, create_info_embed
from utils import to_decimal

logger = logging.getLogger(__name__)


# オートコンプリート用の関数
async def currency_autocomplete(interaction: discord.Interaction, current: str):
    """通貨シンボルのオートコンプリート"""
    if not interaction.guild:
        return []
    
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await fetch_all(
            db,
            "SELECT symbol FROM assets WHERE guild_id = ?",
            (str(interaction.guild.id),)
        )
        symbols = [row[0] for row in rows]
        return [
            app_commands.Choice(name=symbol, value=symbol)
            for symbol in symbols if current.lower() in symbol.lower()
        ][:25]


class MonthlyAllowanceCog(commands.Cog):
    """月次自動送金コマンド群"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_monthly_allowance.start()
    
    def cog_unload(self):
        """Cogアンロード時にタスクを停止"""
        self.check_monthly_allowance.cancel()
    
    @tasks.loop(hours=1)
    async def check_monthly_allowance(self):
        """毎時チェックして、月末（28日）に送金を実行"""
        now = datetime.now(TZ)
        
        # 毎月28日のみ実行
        if now.day != 28:
            return
        
        # 今月まだ実行していない場合のみ実行
        year_month = now.strftime('%Y-%m')
        
        logger.info(f"[MONTHLY_ALLOWANCE] 月次自動送金を開始: {year_month}")
        
        try:
            await self.execute_monthly_allowances(year_month)
            logger.info(f"[MONTHLY_ALLOWANCE] 月次自動送金が完了: {year_month}")
        except Exception as e:
            logger.error(f"[MONTHLY_ALLOWANCE] 月次自動送金でエラーが発生: {e}")
    
    @check_monthly_allowance.before_loop
    async def before_check_monthly_allowance(self):
        """タスク開始前にBotの準備を待つ"""
        await self.bot.wait_until_ready()
    
    async def execute_monthly_allowances(self, year_month: str):
        """月次自動送金を実行"""
        async with aiosqlite.connect(DB_PATH) as db:
            # 有効な設定を取得
            settings = await fetch_all(db, """
                SELECT ma.id, ma.guild_id, ma.role_id, ma.asset_id, ma.amount
                FROM monthly_allowances ma
                WHERE ma.enabled = 1
            """)
            
            total_sent = 0
            total_failed = 0
            
            for setting_id, guild_id, role_id, asset_id, amount in settings:
                guild = self.bot.get_guild(int(guild_id))
                if not guild:
                    logger.warning(f"[MONTHLY_ALLOWANCE] ギルドが見つかりません: {guild_id}")
                    continue
                
                role = guild.get_role(int(role_id))
                if not role:
                    logger.warning(f"[MONTHLY_ALLOWANCE] ロールが見つかりません: {role_id}")
                    continue
                
                # ロールを持つメンバーに送金
                for member in guild.members:
                    if role in member.roles and not member.bot:
                        # 今月まだ送金していないかチェック
                        existing = await fetch_one(db, """
                            SELECT id FROM monthly_allowance_history
                            WHERE guild_id = ? AND role_id = ? AND user_id = ? 
                            AND asset_id = ? AND year_month = ?
                        """, (guild_id, role_id, str(member.id), asset_id, year_month))
                        
                        if not existing:
                            # 送金を実行
                            try:
                                await self.transfer_allowance(
                                    db, guild_id, role_id, member.id, asset_id, amount, year_month
                                )
                                total_sent += 1
                                logger.info(f"[MONTHLY_ALLOWANCE] 送金成功: {member.name} ({member.id}) - {amount}")
                            except Exception as e:
                                total_failed += 1
                                logger.error(f"[MONTHLY_ALLOWANCE] 送金失敗: {member.name} ({member.id}) - {e}")
            
            logger.info(f"[MONTHLY_ALLOWANCE] 送金完了: 成功={total_sent}, 失敗={total_failed}")
    
    async def transfer_allowance(self, db, guild_id: str, role_id: str, user_id: int, asset_id: int, amount: str, year_month: str):
        """月次手当を送金"""
        # ユーザーアカウントを取得/作成
        uid = await upsert_user(db, user_id)
        
        # ユーザーアカウント名を作成
        user_account_name = f"user:{user_id}:{guild_id}"
        
        # アカウントが存在しない場合は作成
        await db.execute(
            "INSERT OR IGNORE INTO accounts(user_id, guild_id, name, type) VALUES (?,?,?, 'user')",
            (uid, guild_id, user_account_name),
        )
        
        # アカウントIDを取得
        user_account_row = await fetch_one(db, "SELECT id FROM accounts WHERE name=?", (user_account_name,))
        user_account_id = int(user_account_row[0])
        
        # Treasuryアカウントを取得
        treasury_account_id = await account_id_by_name(db, 'treasury', int(guild_id))
        
        # Treasury残高を確認し、不足していれば自動補充
        amount_decimal = Decimal(amount)
        await auto_refill_treasury_if_needed(db, treasury_account_id, asset_id, int(guild_id), amount_decimal)
        
        # 送金を実行
        asset_info = await fetch_one(db, "SELECT symbol FROM assets WHERE id = ?", (asset_id,))
        symbol = asset_info[0] if asset_info else "UNKNOWN"
        
        tx_id = await new_transaction(
            db,
            kind='monthly_allowance',
            created_by_user_id=None,
            unique_hash=None,
            reference=f'Monthly allowance: {amount} {symbol}'
        )
        
        # Treasuryから引き出し（マイナス）
        await post_ledger(db, tx_id, treasury_account_id, asset_id, -amount_decimal)
        
        # ユーザーに送金（プラス）
        await post_ledger(db, tx_id, user_account_id, asset_id, amount_decimal)
        
        # 履歴を記録
        await db.execute("""
            INSERT INTO monthly_allowance_history(guild_id, role_id, user_id, asset_id, amount, year_month)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (guild_id, role_id, uid, asset_id, amount, year_month))
        
        await db.commit()
    
    # コマンドグループの作成
    allowance_group = app_commands.Group(
        name="monthly_allowance",
        description="月次自動送金システム（管理者のみ）",
        default_permissions=discord.Permissions(administrator=True)
    )
    
    @allowance_group.command(name="setup", description="月次自動送金を設定（管理者のみ）")
    @app_commands.describe(
        role="対象ロール",
        symbol="通貨シンボル",
        amount="送金額"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    async def setup(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        symbol: str,
        amount: str
    ):
        """月次自動送金を設定"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # 金額の検証
        try:
            amount_decimal = to_decimal(amount)
            if amount_decimal <= 0:
                raise ValueError("金額は正の数である必要があります")
        except Exception as e:
            embed = create_error_embed("入力エラー", f"無効な金額です: {e}", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 通貨が存在するか確認
            asset = await get_asset(db, symbol, interaction.guild.id)
            if not asset:
                embed = create_error_embed("通貨エラー", f"通貨 `{symbol}` が見つかりません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            asset_id = asset[0]
            
            # 設定を追加（既存の場合は更新）
            await db.execute("""
                INSERT INTO monthly_allowances(guild_id, role_id, asset_id, amount, enabled)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(guild_id, role_id, asset_id) 
                DO UPDATE SET amount = ?, enabled = 1
            """, (str(interaction.guild.id), str(role.id), asset_id, str(amount_decimal), str(amount_decimal)))
            
            await db.commit()
        
        embed = create_success_embed(
            "月次自動送金設定完了",
            f"**ロール:** {role.mention}\n"
            f"**通貨:** {symbol}\n"
            f"**金額:** {amount_decimal}\n\n"
            f"毎月28日に、このロールを持つメンバーに自動的に送金されます。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @allowance_group.command(name="list", description="月次自動送金設定一覧を表示（管理者のみ）")
    async def list_allowances(self, interaction: discord.Interaction):
        """月次自動送金設定一覧を表示"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            settings = await fetch_all(db, """
                SELECT ma.role_id, a.symbol, ma.amount, ma.enabled
                FROM monthly_allowances ma
                JOIN assets a ON ma.asset_id = a.id
                WHERE ma.guild_id = ?
                ORDER BY ma.enabled DESC, a.symbol
            """, (str(interaction.guild.id),))
            
            if not settings:
                embed = create_info_embed(
                    "月次自動送金設定",
                    "設定されている月次自動送金はありません。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            embed = discord.Embed(
                title="📅 月次自動送金設定一覧",
                description="毎月28日に自動的に送金される設定です。",
                color=discord.Color.blue()
            )
            
            for role_id, symbol, amount, enabled in settings:
                role = interaction.guild.get_role(int(role_id))
                role_name = role.mention if role else f"<削除済みロール: {role_id}>"
                status = "✅ 有効" if enabled else "❌ 無効"
                
                embed.add_field(
                    name=f"{status} | {role_name}",
                    value=f"**通貨:** {symbol}\n**金額:** {amount}",
                    inline=False
                )
            
            embed.set_footer(text=f"実行者: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @allowance_group.command(name="remove", description="月次自動送金設定を削除（管理者のみ）")
    @app_commands.describe(
        role="対象ロール",
        symbol="通貨シンボル"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    async def remove(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        symbol: str
    ):
        """月次自動送金設定を削除"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 通貨が存在するか確認
            asset = await get_asset(db, symbol, interaction.guild.id)
            if not asset:
                embed = create_error_embed("通貨エラー", f"通貨 `{symbol}` が見つかりません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            asset_id = asset[0]
            
            # 設定を削除
            cursor = await db.execute("""
                DELETE FROM monthly_allowances
                WHERE guild_id = ? AND role_id = ? AND asset_id = ?
            """, (str(interaction.guild.id), str(role.id), asset_id))
            
            deleted = cursor.rowcount
            await db.commit()
            
            if deleted > 0:
                embed = create_success_embed(
                    "月次自動送金設定削除完了",
                    f"**ロール:** {role.mention}\n**通貨:** {symbol}\n\nの設定を削除しました。",
                    interaction.user
                )
            else:
                embed = create_error_embed(
                    "設定エラー",
                    f"指定された設定が見つかりませんでした。",
                    interaction.user
                )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @allowance_group.command(name="enable", description="月次自動送金設定を有効化（管理者のみ）")
    @app_commands.describe(
        role="対象ロール",
        symbol="通貨シンボル"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    async def enable(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        symbol: str
    ):
        """月次自動送金設定を有効化"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 通貨が存在するか確認
            asset = await get_asset(db, symbol, interaction.guild.id)
            if not asset:
                embed = create_error_embed("通貨エラー", f"通貨 `{symbol}` が見つかりません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            asset_id = asset[0]
            
            # 設定を有効化
            cursor = await db.execute("""
                UPDATE monthly_allowances
                SET enabled = 1
                WHERE guild_id = ? AND role_id = ? AND asset_id = ?
            """, (str(interaction.guild.id), str(role.id), asset_id))
            
            updated = cursor.rowcount
            await db.commit()
            
            if updated > 0:
                embed = create_success_embed(
                    "月次自動送金設定有効化完了",
                    f"**ロール:** {role.mention}\n**通貨:** {symbol}\n\nの設定を有効化しました。",
                    interaction.user
                )
            else:
                embed = create_error_embed(
                    "設定エラー",
                    f"指定された設定が見つかりませんでした。",
                    interaction.user
                )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @allowance_group.command(name="disable", description="月次自動送金設定を無効化（管理者のみ）")
    @app_commands.describe(
        role="対象ロール",
        symbol="通貨シンボル"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    async def disable(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        symbol: str
    ):
        """月次自動送金設定を無効化"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 通貨が存在するか確認
            asset = await get_asset(db, symbol, interaction.guild.id)
            if not asset:
                embed = create_error_embed("通貨エラー", f"通貨 `{symbol}` が見つかりません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            asset_id = asset[0]
            
            # 設定を無効化
            cursor = await db.execute("""
                UPDATE monthly_allowances
                SET enabled = 0
                WHERE guild_id = ? AND role_id = ? AND asset_id = ?
            """, (str(interaction.guild.id), str(role.id), asset_id))
            
            updated = cursor.rowcount
            await db.commit()
            
            if updated > 0:
                embed = create_success_embed(
                    "月次自動送金設定無効化完了",
                    f"**ロール:** {role.mention}\n**通貨:** {symbol}\n\nの設定を無効化しました。",
                    interaction.user
                )
            else:
                embed = create_error_embed(
                    "設定エラー",
                    f"指定された設定が見つかりませんでした。",
                    interaction.user
                )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @allowance_group.command(name="history", description="月次自動送金履歴を表示（管理者のみ）")
    @app_commands.describe(year_month="年月（YYYY-MM形式、省略時は今月）")
    async def history(self, interaction: discord.Interaction, year_month: str = None):
        """月次自動送金履歴を表示"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # 年月が指定されていない場合は今月
        if not year_month:
            year_month = datetime.now(TZ).strftime('%Y-%m')
        
        async with aiosqlite.connect(DB_PATH) as db:
            history_records = await fetch_all(db, """
                SELECT h.role_id, h.user_id, a.symbol, h.amount, h.executed_at
                FROM monthly_allowance_history h
                JOIN assets a ON h.asset_id = a.id
                WHERE h.guild_id = ? AND h.year_month = ?
                ORDER BY h.executed_at DESC
                LIMIT 50
            """, (str(interaction.guild.id), year_month))
            
            if not history_records:
                embed = create_info_embed(
                    f"月次自動送金履歴 ({year_month})",
                    f"{year_month}の送金履歴はありません。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            embed = discord.Embed(
                title=f"📊 月次自動送金履歴 ({year_month})",
                description=f"合計 {len(history_records)}件の送金記録",
                color=discord.Color.green()
            )
            
            # ロール毎に集計
            role_summary = {}
            for role_id, user_id, symbol, amount, executed_at in history_records:
                key = (role_id, symbol)
                if key not in role_summary:
                    role_summary[key] = {'count': 0, 'total': Decimal('0')}
                role_summary[key]['count'] += 1
                role_summary[key]['total'] += Decimal(amount)
            
            # 集計結果を表示
            for (role_id, symbol), data in role_summary.items():
                role = interaction.guild.get_role(int(role_id))
                role_name = role.mention if role else f"<削除済みロール: {role_id}>"
                
                embed.add_field(
                    name=f"{role_name} | {symbol}",
                    value=f"**送金数:** {data['count']}人\n**合計金額:** {data['total']}",
                    inline=False
                )
            
            embed.set_footer(text=f"実行者: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @allowance_group.command(name="execute", description="月次自動送金を手動実行（管理者のみ、テスト用）")
    async def execute(self, interaction: discord.Interaction):
        """月次自動送金を手動実行（テスト用）"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        
        year_month = datetime.now(TZ).strftime('%Y-%m')
        
        try:
            await self.execute_monthly_allowances(year_month)
            embed = create_success_embed(
                "月次自動送金実行完了",
                f"{year_month}の月次自動送金を手動実行しました。\n\n"
                f"詳細は `/monthly_allowance history` で確認できます。",
                interaction.user
            )
        except Exception as e:
            embed = create_error_embed(
                "実行エラー",
                f"月次自動送金の実行中にエラーが発生しました:\n```\n{str(e)}\n```",
                interaction.user
            )
        
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    """Cogをセットアップ"""
    await bot.add_cog(MonthlyAllowanceCog(bot))
