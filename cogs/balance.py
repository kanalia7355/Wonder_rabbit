"""
残高・送金Cog

残高確認と送金機能を提供します。
"""

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from decimal import Decimal, ROUND_DOWN

from config import DB_PATH
from database import (
    fetch_all, get_asset, upsert_user,
    ensure_user_account, balance_of,
    new_transaction, post_ledger
)
from embeds import create_error_embed, create_info_embed, create_transaction_embed
from utils import to_decimal


class BalanceCog(commands.Cog):
    """残高・送金コマンド群"""
    
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
            from database import fetch_all
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
    
    @app_commands.command(name="balance", description="自分の残高を表示（自分のみ表示）")
    async def balance(self, interaction: discord.Interaction):
        """自分の全通貨の残高を表示"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            uid = await upsert_user(db, interaction.user.id)
            acc_id = await ensure_user_account(db, interaction.user.id, interaction.guild.id)
            
            # サーバーで作成された通貨のみ表示
            rows = await fetch_all(db, """
                SELECT a.symbol, a.name, a.decimals, COALESCE(SUM(CAST(le.amount AS TEXT)), '0') AS bal
                FROM assets a
                LEFT JOIN ledger_entries le ON le.asset_id = a.id AND le.account_id = ?
                WHERE a.guild_id = ? AND a.symbol != 'COIN'
                GROUP BY a.id
                HAVING bal != '0'
                ORDER BY a.symbol
            """, (acc_id, str(interaction.guild.id)))
            
            if not rows:
                embed = create_info_embed(
                    "残高照会",
                    f"**{interaction.user.display_name}** の残高\n\n現在保有している通貨はありません。",
                    interaction.user
                )
                embed.set_thumbnail(url=interaction.user.display_avatar.url)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            embed = create_info_embed("残高照会", f"**{interaction.user.display_name}** の全通貨残高", interaction.user)
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            
            for sym, name, decimals, bal in rows:
                d = Decimal(bal).quantize(Decimal(10) ** -int(decimals))
                embed.add_field(name=f"{sym} ({name})", value=f"💰 {d:,} {sym}", inline=True)
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name="pay", description="指定ユーザーに送金")
    @app_commands.describe(
        to="送り先ユーザー",
        symbol="通貨シンボル",
        amount="金額",
        memo="メモ（任意）"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    async def pay(
        self,
        interaction: discord.Interaction,
        to: discord.User,
        symbol: str,
        amount: str,
        memo: str = None
    ):
        """指定ユーザーに送金"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        if to.id == interaction.user.id:
            embed = create_error_embed("送金エラー", "自分への送金はできません。", interaction.user)
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
                
                from_acc = await ensure_user_account(db, interaction.user.id, interaction.guild.id)
                to_acc = await ensure_user_account(db, to.id, interaction.guild.id)
                
                bal = await balance_of(db, from_acc, asset_id)
                # 通貨の小数点以下に合わせて金額を丸める
                qamt = amt.quantize(Decimal(10) ** -int(decimals), rounding=ROUND_DOWN)
                
                if bal < qamt:
                    embed = create_error_embed("残高不足", f"残高が不足しています。\n\n現在の残高: {bal} {sym}", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                # 取引を作成
                uid = await upsert_user(db, interaction.user.id)
                tx_id = await new_transaction(db, kind="transfer", created_by_user_id=uid, unique_hash=None, reference=memo or "")
                await post_ledger(db, tx_id, from_acc, asset_id, -qamt)
                await post_ledger(db, tx_id, to_acc, asset_id, qamt)
                await db.commit()
        
        embed = create_transaction_embed(
            "送金",
            interaction.user.mention,
            to.mention,
            str(qamt),
            sym,
            memo,
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    """Cogをセットアップ"""
    await bot.add_cog(BalanceCog(bot))
