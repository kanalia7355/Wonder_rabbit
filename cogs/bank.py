"""
銀行Cog

ユーザーが通貨を銀行に預けたり引き出したりできる機能を提供します。
"""

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from decimal import Decimal
from typing import Optional

from config import DB_PATH
from database import fetch_one, fetch_all, get_asset, upsert_user
from embeds import create_success_embed, create_error_embed, create_info_embed
from utils import to_decimal


class BankCog(commands.Cog):
    """銀行機能コマンド群"""
    
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
                
                choices = [
                    app_commands.Choice(name=f"{symbol} - {name}", value=symbol)
                    for symbol, name in rows
                    if current.upper() in symbol.upper() or current in name
                ]
                
                return choices[:25]
        except:
            return []
    
    bank_group = app_commands.Group(
        name="bank",
        description="銀行機能 - 通貨の預金・引き出し・残高確認"
    )
    
    @bank_group.command(name="deposit", description="通貨を銀行に預ける")
    @app_commands.describe(
        symbol="通貨シンボル",
        amount="預金額"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    async def deposit(
        self,
        interaction: discord.Interaction,
        symbol: str,
        amount: str
    ):
        """銀行に通貨を預ける"""
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
            # 通貨確認
            asset = await get_asset(db, symbol.upper(), interaction.guild.id)
            if not asset:
                embed = create_error_embed("通貨エラー", "指定された通貨が存在しません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            asset_id, sym, _name, decimals = asset
            
            # ユーザーID取得
            user_id = await upsert_user(db, interaction.user.id)
            
            # 金額を丸める
            qamt = amt.quantize(Decimal(10) ** -int(decimals))
            
            # 通常残高を確認
            from database import ensure_user_account, balance_of
            account_id = await ensure_user_account(db, interaction.user.id, interaction.guild.id)
            current_balance = await balance_of(db, account_id, asset_id)
            
            if current_balance < qamt:
                embed = create_error_embed(
                    "残高不足",
                    f"通常残高が不足しています。\n\n"
                    f"現在の残高: **{current_balance.quantize(Decimal(10) ** -int(decimals))} {sym}**\n"
                    f"必要な金額: **{qamt} {sym}**",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # 銀行口座の残高を取得
            bank_row = await fetch_one(db, """
                SELECT balance FROM bank_accounts
                WHERE user_id = ? AND asset_id = ?
            """, (user_id, asset_id))
            
            if bank_row:
                bank_balance = Decimal(bank_row[0])
            else:
                bank_balance = Decimal("0")
                # 銀行口座を作成
                await db.execute("""
                    INSERT INTO bank_accounts(user_id, asset_id, balance)
                    VALUES (?, ?, '0')
                """, (user_id, asset_id))
            
            # 新しい銀行残高
            new_bank_balance = bank_balance + qamt
            
            # トランザクション開始
            async with db.execute("BEGIN"):
                # 通常残高から減算
                from database import new_transaction, post_ledger
                tx_id = await new_transaction(
                    db,
                    kind="bank_deposit",
                    created_by_user_id=user_id,
                    unique_hash=None,
                    reference=f"Bank deposit: {qamt} {sym}"
                )
                await post_ledger(db, tx_id, account_id, asset_id, -qamt)
                
                # 銀行残高を更新
                await db.execute("""
                    UPDATE bank_accounts
                    SET balance = ?
                    WHERE user_id = ? AND asset_id = ?
                """, (str(new_bank_balance), user_id, asset_id))
                
                # 取引履歴に記録
                await db.execute("""
                    INSERT INTO bank_transactions(user_id, asset_id, transaction_type, amount, balance_after)
                    VALUES (?, ?, 'deposit', ?, ?)
                """, (user_id, asset_id, str(qamt), str(new_bank_balance)))
                
                await db.commit()
        
        embed = create_success_embed(
            "預金完了",
            f"**{qamt} {sym}** を銀行に預けました。\n\n"
            f"💰 新しい銀行残高: **{new_bank_balance.quantize(Decimal(10) ** -int(decimals))} {sym}**",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)
    
    @bank_group.command(name="withdraw", description="通貨を銀行から引き出す")
    @app_commands.describe(
        symbol="通貨シンボル",
        amount="引き出し額"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    async def withdraw(
        self,
        interaction: discord.Interaction,
        symbol: str,
        amount: str
    ):
        """銀行から通貨を引き出す"""
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
            # 通貨確認
            asset = await get_asset(db, symbol.upper(), interaction.guild.id)
            if not asset:
                embed = create_error_embed("通貨エラー", "指定された通貨が存在しません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            asset_id, sym, _name, decimals = asset
            
            # ユーザーID取得
            user_id = await upsert_user(db, interaction.user.id)
            
            # 金額を丸める
            qamt = amt.quantize(Decimal(10) ** -int(decimals))
            
            # 銀行残高を確認
            bank_row = await fetch_one(db, """
                SELECT balance FROM bank_accounts
                WHERE user_id = ? AND asset_id = ?
            """, (user_id, asset_id))
            
            if not bank_row:
                embed = create_error_embed(
                    "銀行口座なし",
                    f"**{sym}** の銀行口座がありません。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            bank_balance = Decimal(bank_row[0])
            
            if bank_balance < qamt:
                embed = create_error_embed(
                    "残高不足",
                    f"銀行残高が不足しています。\n\n"
                    f"現在の銀行残高: **{bank_balance.quantize(Decimal(10) ** -int(decimals))} {sym}**\n"
                    f"必要な金額: **{qamt} {sym}**",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # 新しい銀行残高
            new_bank_balance = bank_balance - qamt
            
            # トランザクション開始
            async with db.execute("BEGIN"):
                # 通常残高に加算
                from database import ensure_user_account, new_transaction, post_ledger
                account_id = await ensure_user_account(db, interaction.user.id, interaction.guild.id)
                tx_id = await new_transaction(
                    db,
                    kind="bank_withdraw",
                    created_by_user_id=user_id,
                    unique_hash=None,
                    reference=f"Bank withdraw: {qamt} {sym}"
                )
                await post_ledger(db, tx_id, account_id, asset_id, qamt)
                
                # 銀行残高を更新
                await db.execute("""
                    UPDATE bank_accounts
                    SET balance = ?
                    WHERE user_id = ? AND asset_id = ?
                """, (str(new_bank_balance), user_id, asset_id))
                
                # 取引履歴に記録
                await db.execute("""
                    INSERT INTO bank_transactions(user_id, asset_id, transaction_type, amount, balance_after)
                    VALUES (?, ?, 'withdraw', ?, ?)
                """, (user_id, asset_id, str(qamt), str(new_bank_balance)))
                
                await db.commit()
        
        embed = create_success_embed(
            "引き出し完了",
            f"**{qamt} {sym}** を銀行から引き出しました。\n\n"
            f"💰 新しい銀行残高: **{new_bank_balance.quantize(Decimal(10) ** -int(decimals))} {sym}**",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)
    
    @bank_group.command(name="balance", description="銀行残高を確認する")
    @app_commands.describe(
        symbol="通貨シンボル（省略時は全通貨）"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    async def balance(
        self,
        interaction: discord.Interaction,
        symbol: Optional[str] = None
    ):
        """銀行残高を確認"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            user_id = await upsert_user(db, interaction.user.id)
            
            if symbol:
                # 特定通貨の残高
                asset = await get_asset(db, symbol.upper(), interaction.guild.id)
                if not asset:
                    embed = create_error_embed("通貨エラー", "指定された通貨が存在しません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                asset_id, sym, asset_name, decimals = asset
                
                # 通常残高
                from database import ensure_user_account, balance_of
                account_id = await ensure_user_account(db, interaction.user.id, interaction.guild.id)
                wallet_balance = await balance_of(db, account_id, asset_id)
                
                # 銀行残高
                bank_row = await fetch_one(db, """
                    SELECT balance FROM bank_accounts
                    WHERE user_id = ? AND asset_id = ?
                """, (user_id, asset_id))
                
                bank_balance = Decimal(bank_row[0]) if bank_row else Decimal("0")
                total_balance = wallet_balance + bank_balance
                
                embed = create_info_embed(
                    f"💰 {sym} 残高",
                    f"**{asset_name}** の残高情報\n\n"
                    f"👛 通常残高: **{wallet_balance.quantize(Decimal(10) ** -int(decimals))} {sym}**\n"
                    f"🏦 銀行残高: **{bank_balance.quantize(Decimal(10) ** -int(decimals))} {sym}**\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"💎 合計: **{total_balance.quantize(Decimal(10) ** -int(decimals))} {sym}**",
                    interaction.user
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                # 全通貨の残高
                rows = await fetch_all(db, """
                    SELECT a.id, a.symbol, a.name, a.decimals
                    FROM assets a
                    WHERE a.guild_id = ?
                    ORDER BY a.symbol
                """, (str(interaction.guild.id),))
                
                if not rows:
                    embed = create_info_embed("銀行残高", "このサーバーには通貨が作成されていません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                embed = create_info_embed("💰 全通貨の残高", "通常残高と銀行残高の一覧", interaction.user)
                
                from database import ensure_user_account, balance_of
                account_id = await ensure_user_account(db, interaction.user.id, interaction.guild.id)
                
                for asset_id, sym, name, decimals in rows:
                    # 通常残高
                    wallet_balance = await balance_of(db, account_id, asset_id)
                    
                    # 銀行残高
                    bank_row = await fetch_one(db, """
                        SELECT balance FROM bank_accounts
                        WHERE user_id = ? AND asset_id = ?
                    """, (user_id, asset_id))
                    
                    bank_balance = Decimal(bank_row[0]) if bank_row else Decimal("0")
                    total_balance = wallet_balance + bank_balance
                    
                    embed.add_field(
                        name=f"{sym} ({name})",
                        value=f"👛 {wallet_balance.quantize(Decimal(10) ** -int(decimals))}\n"
                              f"🏦 {bank_balance.quantize(Decimal(10) ** -int(decimals))}\n"
                              f"💎 {total_balance.quantize(Decimal(10) ** -int(decimals))}",
                        inline=True
                    )
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bank_group.command(name="history", description="銀行の取引履歴を表示する")
    @app_commands.describe(
        symbol="通貨シンボル（省略時は全通貨）",
        limit="表示件数（デフォルト: 10）"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    async def history(
        self,
        interaction: discord.Interaction,
        symbol: Optional[str] = None,
        limit: app_commands.Range[int, 1, 50] = 10
    ):
        """銀行取引履歴を表示"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            user_id = await upsert_user(db, interaction.user.id)
            
            if symbol:
                # 特定通貨の履歴
                asset = await get_asset(db, symbol.upper(), interaction.guild.id)
                if not asset:
                    embed = create_error_embed("通貨エラー", "指定された通貨が存在しません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                asset_id, sym, _name, decimals = asset
                
                transactions = await fetch_all(db, """
                    SELECT transaction_type, amount, balance_after, created_at
                    FROM bank_transactions
                    WHERE user_id = ? AND asset_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (user_id, asset_id, limit))
            else:
                # 全通貨の履歴
                transactions = await fetch_all(db, """
                    SELECT bt.transaction_type, bt.amount, bt.balance_after, bt.created_at, a.symbol, a.decimals
                    FROM bank_transactions bt
                    JOIN assets a ON bt.asset_id = a.id
                    WHERE bt.user_id = ?
                    ORDER BY bt.created_at DESC
                    LIMIT ?
                """, (user_id, limit))
            
            if not transactions:
                embed = create_info_embed("取引履歴", "銀行取引履歴がありません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            embed = create_info_embed(
                "📜 銀行取引履歴",
                f"最新 {len(transactions)} 件の取引",
                interaction.user
            )
            
            for tx in transactions:
                if symbol:
                    tx_type, amount, balance_after, created_at = tx
                    sym_display = symbol.upper()
                    dec = decimals
                else:
                    tx_type, amount, balance_after, created_at, sym_display, dec = tx
                
                type_emoji = "📥" if tx_type == "deposit" else "📤"
                type_text = "預金" if tx_type == "deposit" else "引き出し"
                
                amt = Decimal(amount).quantize(Decimal(10) ** -int(dec))
                bal = Decimal(balance_after).quantize(Decimal(10) ** -int(dec))
                
                embed.add_field(
                    name=f"{type_emoji} {type_text} - {sym_display}",
                    value=f"金額: **{amt} {sym_display}**\n"
                          f"残高: {bal} {sym_display}\n"
                          f"日時: {created_at}",
                    inline=False
                )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    # 管理者用コマンド
    @bank_group.command(name="admin_balance", description="【管理者】指定ユーザーの銀行残高を確認")
    @app_commands.describe(
        user="対象ユーザー",
        symbol="通貨シンボル（省略時は全通貨）"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def admin_balance(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        symbol: Optional[str] = None
    ):
        """指定ユーザーの銀行残高を確認（管理者のみ）"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            user_id = await upsert_user(db, user.id)
            
            if symbol:
                # 特定通貨の残高
                asset = await get_asset(db, symbol.upper(), interaction.guild.id)
                if not asset:
                    embed = create_error_embed("通貨エラー", "指定された通貨が存在しません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                asset_id, sym, asset_name, decimals = asset
                
                # 通常残高
                from database import ensure_user_account, balance_of
                account_id = await ensure_user_account(db, user.id, interaction.guild.id)
                wallet_balance = await balance_of(db, account_id, asset_id)
                
                # 銀行残高
                bank_row = await fetch_one(db, """
                    SELECT balance FROM bank_accounts
                    WHERE user_id = ? AND asset_id = ?
                """, (user_id, asset_id))
                
                bank_balance = Decimal(bank_row[0]) if bank_row else Decimal("0")
                total_balance = wallet_balance + bank_balance
                
                embed = create_info_embed(
                    f"💰 {user.display_name} の {sym} 残高",
                    f"**{asset_name}** の残高情報\n\n"
                    f"👛 通常残高: **{wallet_balance.quantize(Decimal(10) ** -int(decimals))} {sym}**\n"
                    f"🏦 銀行残高: **{bank_balance.quantize(Decimal(10) ** -int(decimals))} {sym}**\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"💎 合計: **{total_balance.quantize(Decimal(10) ** -int(decimals))} {sym}**",
                    interaction.user
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                # 全通貨の残高
                rows = await fetch_all(db, """
                    SELECT a.id, a.symbol, a.name, a.decimals
                    FROM assets a
                    WHERE a.guild_id = ?
                    ORDER BY a.symbol
                """, (str(interaction.guild.id),))
                
                if not rows:
                    embed = create_info_embed("銀行残高", "このサーバーには通貨が作成されていません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                embed = create_info_embed(
                    f"💰 {user.display_name} の全通貨残高",
                    "通常残高と銀行残高の一覧",
                    interaction.user
                )
                
                from database import ensure_user_account, balance_of
                account_id = await ensure_user_account(db, user.id, interaction.guild.id)
                
                for asset_id, sym, name, decimals in rows:
                    # 通常残高
                    wallet_balance = await balance_of(db, account_id, asset_id)
                    
                    # 銀行残高
                    bank_row = await fetch_one(db, """
                        SELECT balance FROM bank_accounts
                        WHERE user_id = ? AND asset_id = ?
                    """, (user_id, asset_id))
                    
                    bank_balance = Decimal(bank_row[0]) if bank_row else Decimal("0")
                    total_balance = wallet_balance + bank_balance
                    
                    embed.add_field(
                        name=f"{sym} ({name})",
                        value=f"👛 {wallet_balance.quantize(Decimal(10) ** -int(decimals))}\n"
                              f"🏦 {bank_balance.quantize(Decimal(10) ** -int(decimals))}\n"
                              f"💎 {total_balance.quantize(Decimal(10) ** -int(decimals))}",
                        inline=True
                    )
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bank_group.command(name="admin_history", description="【管理者】指定ユーザーの取引履歴を表示")
    @app_commands.describe(
        user="対象ユーザー",
        symbol="通貨シンボル（省略時は全通貨）",
        limit="表示件数（デフォルト: 10）"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def admin_history(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        symbol: Optional[str] = None,
        limit: app_commands.Range[int, 1, 50] = 10
    ):
        """指定ユーザーの取引履歴を表示（管理者のみ）"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            user_id = await upsert_user(db, user.id)
            
            if symbol:
                # 特定通貨の履歴
                asset = await get_asset(db, symbol.upper(), interaction.guild.id)
                if not asset:
                    embed = create_error_embed("通貨エラー", "指定された通貨が存在しません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                asset_id, sym, _name, decimals = asset
                
                transactions = await fetch_all(db, """
                    SELECT transaction_type, amount, balance_after, created_at
                    FROM bank_transactions
                    WHERE user_id = ? AND asset_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (user_id, asset_id, limit))
            else:
                # 全通貨の履歴
                transactions = await fetch_all(db, """
                    SELECT bt.transaction_type, bt.amount, bt.balance_after, bt.created_at, a.symbol, a.decimals
                    FROM bank_transactions bt
                    JOIN assets a ON bt.asset_id = a.id
                    WHERE bt.user_id = ?
                    ORDER BY bt.created_at DESC
                    LIMIT ?
                """, (user_id, limit))
            
            if not transactions:
                embed = create_info_embed("取引履歴", f"{user.display_name} の銀行取引履歴がありません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            embed = create_info_embed(
                f"📜 {user.display_name} の取引履歴",
                f"最新 {len(transactions)} 件の取引",
                interaction.user
            )
            
            for tx in transactions:
                if symbol:
                    tx_type, amount, balance_after, created_at = tx
                    sym_display = symbol.upper()
                    dec = decimals
                else:
                    tx_type, amount, balance_after, created_at, sym_display, dec = tx
                
                type_emoji = "📥" if tx_type == "deposit" else "📤"
                type_text = "預金" if tx_type == "deposit" else "引き出し"
                
                amt = Decimal(amount).quantize(Decimal(10) ** -int(dec))
                bal = Decimal(balance_after).quantize(Decimal(10) ** -int(dec))
                
                embed.add_field(
                    name=f"{type_emoji} {type_text} - {sym_display}",
                    value=f"金額: **{amt} {sym_display}**\n"
                          f"残高: {bal} {sym_display}\n"
                          f"日時: {created_at}",
                    inline=False
                )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bank_group.command(name="admin_search", description="【管理者】銀行取引を検索")
    @app_commands.describe(
        transaction_type="取引種別（deposit/withdraw）",
        symbol="通貨シンボル（省略時は全通貨）",
        user="対象ユーザー（省略時は全ユーザー）",
        limit="表示件数（デフォルト: 20）"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def admin_search(
        self,
        interaction: discord.Interaction,
        transaction_type: Optional[str] = None,
        symbol: Optional[str] = None,
        user: Optional[discord.User] = None,
        limit: app_commands.Range[int, 1, 100] = 20
    ):
        """取引を検索（管理者のみ）"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # 検索条件を構築
        conditions = []
        params = []
        
        if transaction_type:
            if transaction_type not in ["deposit", "withdraw"]:
                embed = create_error_embed("入力エラー", "取引種別は 'deposit' または 'withdraw' を指定してください。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            conditions.append("bt.transaction_type = ?")
            params.append(transaction_type)
        
        async with aiosqlite.connect(DB_PATH) as db:
            if symbol:
                asset = await get_asset(db, symbol.upper(), interaction.guild.id)
                if not asset:
                    embed = create_error_embed("通貨エラー", "指定された通貨が存在しません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                asset_id = asset[0]
                conditions.append("bt.asset_id = ?")
                params.append(asset_id)
            
            if user:
                user_id = await upsert_user(db, user.id)
                conditions.append("bt.user_id = ?")
                params.append(user_id)
            
            # WHERE句を構築
            where_clause = " AND ".join(conditions) if conditions else "1=1"
            
            # クエリ実行
            query = f"""
                SELECT bt.transaction_type, bt.amount, bt.balance_after, bt.created_at,
                       a.symbol, a.decimals, u.discord_user_id
                FROM bank_transactions bt
                JOIN assets a ON bt.asset_id = a.id
                JOIN users u ON bt.user_id = u.id
                WHERE {where_clause}
                ORDER BY bt.created_at DESC
                LIMIT ?
            """
            params.append(limit)
            
            transactions = await fetch_all(db, query, tuple(params))
            
            if not transactions:
                embed = create_info_embed("検索結果", "条件に一致する取引が見つかりませんでした。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # 検索条件を表示
            search_info = []
            if transaction_type:
                search_info.append(f"種別: {transaction_type}")
            if symbol:
                search_info.append(f"通貨: {symbol.upper()}")
            if user:
                search_info.append(f"ユーザー: {user.display_name}")
            
            embed = create_info_embed(
                "🔍 取引検索結果",
                f"検索条件: {', '.join(search_info) if search_info else '全て'}\n"
                f"結果: {len(transactions)} 件",
                interaction.user
            )
            
            for tx in transactions:
                tx_type, amount, balance_after, created_at, sym_display, dec, discord_user_id = tx
                
                # ユーザー情報を取得
                try:
                    tx_user = await self.bot.fetch_user(int(discord_user_id))
                    user_name = tx_user.display_name
                except:
                    user_name = f"User#{discord_user_id}"
                
                type_emoji = "📥" if tx_type == "deposit" else "📤"
                type_text = "預金" if tx_type == "deposit" else "引き出し"
                
                amt = Decimal(amount).quantize(Decimal(10) ** -int(dec))
                bal = Decimal(balance_after).quantize(Decimal(10) ** -int(dec))
                
                embed.add_field(
                    name=f"{type_emoji} {type_text} - {sym_display} ({user_name})",
                    value=f"金額: **{amt} {sym_display}**\n"
                          f"残高: {bal} {sym_display}\n"
                          f"日時: {created_at}",
                    inline=False
                )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    """Cogをセットアップ"""
    await bot.add_cog(BankCog(bot))
