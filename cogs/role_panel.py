"""
ロール購入パネルCog

期限付きロールの購入システムを提供します。
"""

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from decimal import Decimal
from datetime import datetime, timedelta

from config import DB_PATH, TZ
from database import fetch_one, fetch_all, get_asset, upsert_user, ensure_user_account, balance_of, account_id_by_name, new_transaction, post_ledger
from embeds import create_success_embed, create_error_embed, create_info_embed
from utils import to_decimal


class RolePanelCog(commands.Cog):
    """ロール購入パネルコマンド群"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    async def panel_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """パネル名のオートコンプリート"""
        if not interaction.guild:
            return []
        
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                rows = await fetch_all(db, """
                    SELECT DISTINCT panel_name FROM role_panels
                    WHERE guild_id = ?
                    ORDER BY panel_name
                """, (str(interaction.guild.id),))
                
                choices = [
                    app_commands.Choice(name=panel_name, value=panel_name)
                    for (panel_name,) in rows
                    if current.lower() in panel_name.lower()
                ]
                
                return choices[:25]
        except:
            return []
    
    async def currency_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """通貨シンボルのオートコンプリート"""
        if not interaction.guild:
            return []
        
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                rows = await fetch_all(db, """
                    SELECT symbol, name FROM assets
                    WHERE guild_id = ?
                    ORDER BY symbol
                """, (str(interaction.guild.id),))
                
                choices = [
                    app_commands.Choice(name=f"{symbol} - {name}", value=symbol)
                    for symbol, name in rows
                    if current.upper() in symbol.upper() or current in name
                ]
                
                return choices[:25]
        except:
            return []
    
    @app_commands.command(name="panel_create", description="ロール購入パネルを作成する（管理者のみ）")
    @app_commands.describe(
        panel_name="パネル名",
        description="パネルの説明"
    )
    @app_commands.default_permissions(manage_guild=True)
    async def panel_create(
        self,
        interaction: discord.Interaction,
        panel_name: str,
        description: str = None
    ):
        """ロール購入パネルを作成"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 既存チェック
            existing = await fetch_one(db, """
                SELECT panel_id FROM role_panels
                WHERE guild_id = ? AND panel_name = ?
                LIMIT 1
            """, (str(interaction.guild.id), panel_name))
            
            if existing:
                embed = create_error_embed("パネル作成エラー", f"パネル **{panel_name}** は既に存在します。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # 新しいパネルIDを生成
            max_id_row = await fetch_one(db, """
                SELECT MAX(panel_id) FROM role_panels WHERE guild_id = ?
            """, (str(interaction.guild.id),))
            
            new_panel_id = (max_id_row[0] or 0) + 1 if max_id_row else 1
            
            # パネルを作成（プレースホルダーとして1行挿入）
            await db.execute("""
                INSERT INTO role_panels(guild_id, panel_id, panel_name, role_id, currency_symbol)
                VALUES (?, ?, ?, '', '')
            """, (str(interaction.guild.id), new_panel_id, panel_name))
            await db.commit()
        
        embed = create_success_embed(
            "パネル作成完了",
            f"ロール購入パネル **{panel_name}** を作成しました。\n\n"
            f"• パネルID: {new_panel_id}\n"
            f"• 説明: {description or 'なし'}\n\n"
            f"次に `/ロールプラン追加` でプランを追加してください。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="plan_add", description="パネルにプランを追加する（管理者のみ）")
    @app_commands.describe(
        panel_name="パネル名",
        plan_name="プラン名",
        role="付与するロール",
        price="価格",
        symbol="通貨シンボル",
        duration_hours="期限（時間数）",
        description="プランの説明（任意）"
    )
    @app_commands.autocomplete(panel_name=panel_autocomplete, symbol=currency_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def plan_add(
        self,
        interaction: discord.Interaction,
        panel_name: str,
        plan_name: str,
        role: discord.Role,
        price: str,
        symbol: str,
        duration_hours: int,
        description: str = None
    ):
        """パネルにプランを追加"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # 価格のバリデーション
        try:
            price_decimal = to_decimal(price)
            if price_decimal <= 0:
                raise ValueError
        except ValueError:
            embed = create_error_embed("入力エラー", "価格は0より大きい数値を入力してください。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # パネルの存在確認
            panel = await fetch_one(db, """
                SELECT panel_id FROM role_panels
                WHERE guild_id = ? AND panel_name = ?
                LIMIT 1
            """, (str(interaction.guild.id), panel_name))
            
            if not panel:
                embed = create_error_embed("パネルエラー", f"パネル **{panel_name}** が見つかりません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            panel_id = panel[0]
            
            # 通貨の存在確認
            asset = await get_asset(db, symbol.upper(), interaction.guild.id)
            if not asset:
                embed = create_error_embed("通貨エラー", f"通貨 **{symbol}** が存在しません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # プランを追加
            await db.execute("""
                INSERT INTO role_plans(panel_id, plan_name, role_id, price, currency_symbol, duration_hours, description, guild_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (panel_id, plan_name, str(role.id), str(price_decimal), symbol.upper(), duration_hours, description or "", str(interaction.guild.id)))
            
            # role_panelsテーブルも更新
            await db.execute("""
                UPDATE role_panels
                SET role_id = ?, currency_symbol = ?
                WHERE guild_id = ? AND panel_id = ?
            """, (str(role.id), symbol.upper(), str(interaction.guild.id), panel_id))
            
            await db.commit()
        
        hours_text = f"{duration_hours}時間"
        if duration_hours >= 24:
            days = duration_hours // 24
            remaining_hours = duration_hours % 24
            hours_text = f"{days}日" + (f"{remaining_hours}時間" if remaining_hours > 0 else "")
        
        embed = create_success_embed(
            "プラン追加完了",
            f"パネル **{panel_name}** にプランを追加しました。\n\n"
            f"• プラン名: {plan_name}\n"
            f"• ロール: {role.mention}\n"
            f"• 価格: {price_decimal} {symbol.upper()}\n"
            f"• 期限: {hours_text}\n"
            f"• 説明: {description or 'なし'}",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="plan_list", description="パネルのプラン一覧を表示する（管理者のみ）")
    @app_commands.describe(panel_name="パネル名")
    @app_commands.autocomplete(panel_name=panel_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def plan_list(self, interaction: discord.Interaction, panel_name: str):
        """パネルのプラン一覧を表示"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # パネルの存在確認
            panel = await fetch_one(db, """
                SELECT panel_id FROM role_panels
                WHERE guild_id = ? AND panel_name = ?
                LIMIT 1
            """, (str(interaction.guild.id), panel_name))
            
            if not panel:
                embed = create_error_embed("パネルエラー", f"パネル **{panel_name}** が見つかりません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            panel_id = panel[0]
            
            # プラン一覧を取得
            plans = await fetch_all(db, """
                SELECT id, plan_name, role_id, price, currency_symbol, duration_hours, description
                FROM role_plans
                WHERE panel_id = ?
                ORDER BY id
            """, (panel_id,))
            
            if not plans:
                embed = create_info_embed(
                    "プラン一覧",
                    f"パネル **{panel_name}** にはまだプランがありません。\n\n"
                    f"`/ロールプラン追加` でプランを追加してください。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        embed = create_info_embed(
            f"プラン一覧 - {panel_name}",
            f"パネル **{panel_name}** のプラン（{len(plans)}件）",
            interaction.user
        )
        
        for plan_id, plan_name_db, role_id, price, currency, duration_hours, desc in plans:
            role = interaction.guild.get_role(int(role_id))
            role_text = role.mention if role else f"(削除済み: {role_id})"
            
            hours_text = f"{duration_hours}時間"
            if duration_hours >= 24:
                days = duration_hours // 24
                remaining_hours = duration_hours % 24
                hours_text = f"{days}日" + (f"{remaining_hours}時間" if remaining_hours > 0 else "")
            
            embed.add_field(
                name=f"#{plan_id}: {plan_name_db}",
                value=f"ロール: {role_text}\n価格: {price} {currency}\n期限: {hours_text}\n説明: {desc or 'なし'}",
                inline=False
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="panel_list", description="全てのパネル一覧を表示する（管理者のみ）")
    @app_commands.default_permissions(manage_guild=True)
    async def panel_list(self, interaction: discord.Interaction):
        """全てのパネル一覧を表示"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            panels = await fetch_all(db, """
                SELECT DISTINCT panel_id, panel_name
                FROM role_panels
                WHERE guild_id = ?
                ORDER BY panel_id
            """, (str(interaction.guild.id),))
            
            if not panels:
                embed = create_info_embed(
                    "パネル一覧",
                    "まだロール購入パネルが作成されていません。\n\n"
                    "`/ロールパネル作成` でパネルを作成してください。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        embed = create_info_embed(
            "パネル一覧",
            f"ロール購入パネル（{len(panels)}件）",
            interaction.user
        )
        
        for panel_id, panel_name in panels:
            # プラン数を取得
            async with aiosqlite.connect(DB_PATH) as db:
                plan_count_row = await fetch_one(db, """
                    SELECT COUNT(*) FROM role_plans WHERE panel_id = ?
                """, (panel_id,))
                plan_count = plan_count_row[0] if plan_count_row else 0
            
            embed.add_field(
                name=f"#{panel_id}: {panel_name}",
                value=f"プラン数: {plan_count}件",
                inline=True
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="panel_delete", description="パネルを削除する（管理者のみ）")
    @app_commands.describe(panel_name="パネル名")
    @app_commands.autocomplete(panel_name=panel_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def panel_delete(self, interaction: discord.Interaction, panel_name: str):
        """パネルを削除"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # パネルの存在確認
            panel = await fetch_one(db, """
                SELECT panel_id FROM role_panels
                WHERE guild_id = ? AND panel_name = ?
                LIMIT 1
            """, (str(interaction.guild.id), panel_name))
            
            if not panel:
                embed = create_error_embed("パネルエラー", f"パネル **{panel_name}** が見つかりません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            panel_id = panel[0]
            
            # 関連データを削除
            await db.execute("DELETE FROM role_plans WHERE panel_id = ?", (panel_id,))
            await db.execute("DELETE FROM role_panels WHERE guild_id = ? AND panel_id = ?", (str(interaction.guild.id), panel_id))
            await db.execute("DELETE FROM deployed_panels WHERE panel_id = ?", (panel_id,))
            await db.commit()
        
        embed = create_success_embed(
            "パネル削除完了",
            f"パネル **{panel_name}** を削除しました。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="panel_deploy", description="パネルを設置する（管理者のみ）")
    @app_commands.describe(panel_name="パネル名")
    @app_commands.autocomplete(panel_name=panel_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def panel_deploy(self, interaction: discord.Interaction, panel_name: str):
        """パネルを設置"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # パネルの存在確認
            panel = await fetch_one(db, """
                SELECT panel_id, role_id, currency_symbol FROM role_panels
                WHERE guild_id = ? AND panel_name = ?
                LIMIT 1
            """, (str(interaction.guild.id), panel_name))
            
            if not panel:
                embed = create_error_embed("パネルエラー", f"パネル **{panel_name}** が見つかりません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            panel_id, role_id, currency_symbol = panel
            
            # プラン一覧を取得
            plans = await fetch_all(db, """
                SELECT id, plan_name, role_id, price, currency_symbol, duration_hours, description
                FROM role_plans
                WHERE panel_id = ?
                ORDER BY price
            """, (panel_id,))
            
            if not plans:
                embed = create_error_embed(
                    "プランエラー",
                    f"パネル **{panel_name}** にはプランがありません。\n\n"
                    f"`/プラン追加` でプランを追加してください。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # パネルの埋め込みを作成
        embed = discord.Embed(
            title=f"{panel_name}",
            description="以下のプランから選択して購入できます。",
            color=discord.Color.blue()
        )
        
        # プラン情報を1行ずつ追加
        plan_lines = []
        for plan_id, plan_name_db, plan_role_id, price, plan_currency, duration_hours, desc in plans:
            hours_text = f"{duration_hours}時間"
            if duration_hours >= 24:
                days = duration_hours // 24
                remaining_hours = duration_hours % 24
                hours_text = f"{days}日" + (f"{remaining_hours}時間" if remaining_hours > 0 else "")
            
            plan_line = f"**{hours_text}**: {price} {plan_currency}"
            if desc:
                plan_line += f" - {desc}"
            plan_lines.append(plan_line)
        
        # プラン一覧を説明に追加
        embed.description = "以下のプランから選択して購入できます。\n\n" + "\n".join(plan_lines)
        embed.set_footer(text="下のボタンをクリックして購入してください。")
        
        # パネルを投稿
        try:
            # RolePurchaseViewをインポート
            from models import RolePurchaseView
            
            # Viewを作成（panel_idを渡す）
            view = RolePurchaseView(panel_id=panel_id)
            
            # 現在のチャンネルに投稿
            message = await interaction.channel.send(embed=embed, view=view)
            
            # deployed_panelsテーブルに記録
            async with aiosqlite.connect(DB_PATH) as db:
                # role_panelsテーブルのidを取得
                panel_db_row = await fetch_one(db, """
                    SELECT id FROM role_panels 
                    WHERE guild_id = ? AND panel_id = ?
                    LIMIT 1
                """, (str(interaction.guild.id), panel_id))
                
                if panel_db_row:
                    panel_db_id = panel_db_row[0]
                    await db.execute("""
                        INSERT INTO deployed_panels(panel_db_id, guild_id, channel_id, message_id)
                        VALUES (?, ?, ?, ?)
                    """, (panel_db_id, str(interaction.guild.id), str(interaction.channel.id), str(message.id)))
                    await db.commit()
            
            # 成功メッセージ
            embed = create_success_embed(
                "パネル設置完了",
                f"パネル **{panel_name}** を設置しました。\n\n"
                f"• チャンネル: {interaction.channel.mention}\n"
                f"• プラン数: {len(plans)}件",
                interaction.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            embed = create_error_embed(
                "設置エラー",
                f"パネルの設置中にエラーが発生しました: {str(e)}",
                interaction.user
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    """Cogをセットアップ"""
    await bot.add_cog(RolePanelCog(bot))
