"""
通貨管理Cog

通貨の作成、削除、Treasury管理、ランキングなどの機能を提供します。
"""

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from decimal import Decimal

from config import DB_PATH, DEFAULT_DECIMALS
from database import (
    fetch_one, fetch_all, get_asset, create_asset,
    ensure_system_accounts, ensure_guild_setup,
    account_id_by_name, balance_of, upsert_user,
    ensure_user_account, new_transaction, post_ledger
)
from embeds import create_success_embed, create_error_embed, create_info_embed, create_transaction_embed
from utils import to_decimal
from models import CurrencyDeleteConfirmView


class CurrencyCog(commands.Cog):
    """通貨管理コマンド群"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
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
                
                # 現在の入力にマッチする通貨をフィルタ
                choices = [
                    app_commands.Choice(name=f"{symbol} - {name}", value=symbol)
                    for symbol, name in rows
                    if current.upper() in symbol.upper() or current in name
                ]
                
                return choices[:25]  # Discord制限: 最大25個
        except:
            return []
    
    @app_commands.command(name="create", description="新しい通貨を作成（管理者のみ）")
    @app_commands.describe(
        symbol="通貨シンボル（例: GOLD）",
        name="通貨名（例: ゴールドコイン）",
        decimals="小数点以下の桁数（既定=2）"
    )
    @app_commands.default_permissions(manage_guild=True)
    async def create_currency(
        self,
        interaction: discord.Interaction,
        symbol: str,
        name: str,
        decimals: app_commands.Range[int, 0, 8] = DEFAULT_DECIMALS
    ):
        """新しい通貨を作成"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        symbol = symbol.upper()
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 既存チェック
            existing = await get_asset(db, symbol, interaction.guild.id)
            if existing:
                embed = create_error_embed("通貨作成エラー", f"通貨 **{symbol}** は既に存在します。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # 通貨作成
            await create_asset(db, symbol, name, interaction.guild.id, decimals)
            await ensure_system_accounts(db, interaction.guild.id)
            
            # 作成した通貨を取得
            asset = await get_asset(db, symbol, interaction.guild.id)
            asset_id = asset[0]
            
            # Treasuryアカウントを取得
            treasury_acc = await account_id_by_name(db, "treasury", interaction.guild.id)
            
            # 初期値10億をTreasuryに発行
            from decimal import Decimal
            initial_amount = Decimal("1000000000")
            uid = await upsert_user(db, interaction.user.id)
            tx_id = await new_transaction(
                db,
                kind="initial_issue",
                created_by_user_id=uid,
                unique_hash=None,
                reference=f"Initial treasury balance for {symbol}"
            )
            await post_ledger(db, tx_id, treasury_acc, asset_id, initial_amount)
            
            await db.commit()
        
        embed = create_success_embed(
            "通貨作成完了",
            f"**{symbol}** ({name}) を作成しました。\n\n"
            f"• 小数点以下: {decimals}桁\n"
            f"• Treasury: 自動作成済み\n\n"
            f"初期残高: **1,000,000,000 {symbol}**\n\n💡 Treasuryの残高が0になると、自動的に10億{symbol}が補充されます。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)
    
    @app_commands.command(name="delete", description="通貨を削除（管理者のみ・確認ボタン付き）")
    @app_commands.describe(symbol="削除する通貨シンボル")
    @app_commands.autocomplete(symbol=currency_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def delete_currency(self, interaction: discord.Interaction, symbol: str):
        """通貨を削除（確認ボタン付き）"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        symbol = symbol.upper()
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 通貨の存在確認
            asset = await get_asset(db, symbol, interaction.guild.id)
            if not asset:
                embed = create_error_embed("通貨エラー", f"通貨 **{symbol}** が見つかりません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            asset_id, sym, asset_name, decimals = asset
            
            # 残高情報を取得
            balance_info = await fetch_all(db, """
                SELECT account_id, SUM(CAST(amount AS TEXT)) as balance
                FROM ledger_entries
                WHERE asset_id = ?
                GROUP BY account_id
                HAVING balance != '0'
            """, (asset_id,))
            
            # 請求情報を取得
            claim_count_row = await fetch_one(db, "SELECT COUNT(*) FROM claims WHERE asset_id = ?", (asset_id,))
            claim_count = claim_count_row[0] if claim_count_row else 0
        
        # 確認メッセージ
        warning_text = f"**⚠️ 警告: この操作は取り消せません**\n\n"
        warning_text += f"通貨 **{symbol}** ({asset_name}) を完全に削除します。\n\n"
        
        if balance_info:
            warning_text += f"**影響を受けるデータ:**\n"
            warning_text += f"• 残高を持つアカウント: {len(balance_info)}件\n"
        if claim_count > 0:
            warning_text += f"• 関連する請求: {claim_count}件\n"
        
        warning_text += f"\n本当に削除しますか？"
        
        embed = discord.Embed(
            title="🗑️ 通貨削除の確認",
            description=warning_text,
            color=0xe74c3c
        )
        embed.set_footer(text=f"実行者: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
        
        view = CurrencyDeleteConfirmView(symbol, interaction.guild.id, interaction.user, asset, balance_info, claim_count)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @app_commands.command(name="treasury", description="Treasury残高を確認（管理者のみ）")
    @app_commands.describe(
        symbol="通貨シンボル（省略時は全通貨表示）",
        hidden="他のユーザーに表示しない（既定=True）"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def treasury_balance(
        self,
        interaction: discord.Interaction,
        symbol: str = None,
        hidden: bool = True
    ):
        """Treasury残高を確認"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        await ensure_guild_setup(interaction.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            treasury_acc = await account_id_by_name(db, "treasury", interaction.guild.id)
            
            if symbol:
                # 特定通貨のTreasury残高
                asset = await get_asset(db, symbol.upper(), interaction.guild.id)
                if not asset:
                    embed = create_error_embed("通貨エラー", "指定された通貨が存在しません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                asset_id, sym, asset_name, decimals = asset
                bal = await balance_of(db, treasury_acc, asset_id)
                q = f"{bal.quantize(Decimal(10) ** -decimals)} {sym}"
                
                embed = create_info_embed("Treasury残高", f"**{sym}** ({asset_name}) のTreasury残高\n\n🏦 **{q}**", interaction.user)
                await interaction.response.send_message(embed=embed, ephemeral=hidden)
            else:
                # 全通貨のTreasury残高
                rows = await fetch_all(db, """
                    SELECT a.symbol, a.name, a.decimals, COALESCE(SUM(CAST(le.amount AS TEXT)), '0') AS bal
                    FROM assets a
                    LEFT JOIN ledger_entries le ON le.asset_id = a.id AND le.account_id = ?
                    WHERE a.guild_id = ?
                    GROUP BY a.id, a.symbol, a.name
                    ORDER BY a.symbol
                """, (treasury_acc, str(interaction.guild.id)))
                
                if not rows:
                    embed = create_info_embed("Treasury残高", "このサーバーには通貨が作成されていません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=hidden)
                
                embed = create_info_embed("Treasury残高", "全通貨のTreasury残高", interaction.user)
                
                total_positive = 0
                for sym, name, decimals, bal in rows:
                    d = Decimal(bal).quantize(Decimal(10) ** -int(decimals))
                    status = "🟢" if d > 0 else "🔴" if d < 0 else "⚪"
                    embed.add_field(name=f"{status} {sym} ({name})", value=f"🏦 {d} {sym}", inline=True)
                    if d > 0:
                        total_positive += 1
                
                embed.add_field(name="📊 統計", value=f"総通貨数: {len(rows)}\n残高あり: {total_positive}", inline=False)
                await interaction.response.send_message(embed=embed, ephemeral=hidden)
    
    @app_commands.command(name="give", description="Treasuryから発行（管理者のみ）")
    @app_commands.describe(
        to="発行先ユーザー",
        symbol="通貨シンボル",
        amount="発行額",
        memo="メモ（任意）"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def give_currency(
        self,
        interaction: discord.Interaction,
        to: discord.User,
        symbol: str,
        amount: str,
        memo: str = None
    ):
        """Treasuryから通貨を発行"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        try:
            amt = to_decimal(amount)
            if amt <= 0:
                raise ValueError
        except ValueError:
            embed = create_error_embed("入力エラー", "金額は0より大きい数値を入力してください。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("BEGIN"):
                asset = await get_asset(db, symbol.upper(), interaction.guild.id)
                if not asset:
                    embed = create_error_embed("通貨エラー", "指定された通貨が存在しません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                asset_id, sym, _name, decimals = asset
                
                treasury_acc = await account_id_by_name(db, "treasury", interaction.guild.id)
                to_acc = await ensure_user_account(db, to.id, interaction.guild.id)
                
                # 金額を丸める
                qamt = amt.quantize(Decimal(10) ** -int(decimals))
                
                # Treasuryの残高をチェックして、必要なら自動補充
                from database import auto_refill_treasury_if_needed
                await auto_refill_treasury_if_needed(db, treasury_acc, asset_id, interaction.guild.id, qamt)
                
                # 取引を作成
                uid = await upsert_user(db, interaction.user.id)
                tx_id = await new_transaction(db, kind="issue", created_by_user_id=uid, unique_hash=None, reference=memo or "")
                await post_ledger(db, tx_id, treasury_acc, asset_id, -qamt)
                await post_ledger(db, tx_id, to_acc, asset_id, qamt)
                await db.commit()
        
        embed = create_transaction_embed(
            "発行",
            "🏦 Treasury",
            to.mention,
            str(qamt),
            sym,
            memo,
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    """Cogをセットアップ"""
    await bot.add_cog(CurrencyCog(bot))
