"""
自動報酬Cog

メッセージリアクションによる自動報酬システムを提供します。
"""

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from datetime import datetime
from decimal import Decimal

from config import DB_PATH, TZ
from database import (
    fetch_one, fetch_all, get_asset, upsert_user,
    ensure_user_account, balance_of, account_id_by_name,
    auto_refill_treasury_if_needed, new_transaction, post_ledger,
    get_asset_info_by_id
)
from embeds import create_success_embed, create_error_embed, create_info_embed
from utils import to_decimal


class AutoRewardCog(commands.Cog):
    """自動報酬システムコマンド群"""
    
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
    
    # コマンドグループの作成
    autoreward_group = app_commands.Group(
        name="autoreward",
        description="メッセージトリガー自動報酬システム（管理者のみ）",
        default_permissions=discord.Permissions(manage_guild=True)
    )
    
    @autoreward_group.command(name="setup", description="特定メッセージで通貨を自動付与する設定（管理者）")
    @app_commands.describe(
        channel="対象チャンネル",
        trigger_message="トリガーとなるメッセージ",
        amount="付与する金額",
        symbol="通貨シンボル"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    async def setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        trigger_message: str,
        amount: str,
        symbol: str
    ):
        """特定メッセージで通貨を自動付与する設定"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # トリガーメッセージのバリデーション
        if not trigger_message or not trigger_message.strip():
            embed = create_error_embed("入力エラー", "トリガーメッセージを入力してください。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        if len(trigger_message) > 500:
            embed = create_error_embed("入力エラー", "トリガーメッセージは500文字以内で入力してください。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # 金額のバリデーション
        try:
            if not amount or not amount.strip():
                raise ValueError("Empty input")
            
            amount_decimal = to_decimal(amount)
            if amount_decimal <= 0:
                raise ValueError("Non-positive value")
            if not amount_decimal.is_finite():
                raise ValueError("Not finite")
        except ValueError:
            embed = create_error_embed("入力エラー", "金額は0より大きい有効な数値を入力してください。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 通貨の確認
            asset = await get_asset(db, symbol, interaction.guild.id)
            if not asset:
                embed = create_error_embed("通貨エラー", "指定された通貨が存在しません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            asset_id = asset[0]
            
            # 既存設定の確認
            existing = await fetch_one(db, """
                SELECT id, trigger_message, reward_amount, enabled FROM auto_reward_configs
                WHERE guild_id = ? AND channel_id = ?
            """, (str(interaction.guild.id), str(channel.id)))
            
            if existing:
                # 既存設定がある場合は上書き
                await db.execute("""
                    UPDATE auto_reward_configs
                    SET trigger_message = ?, reward_amount = ?, asset_id = ?, enabled = 1
                    WHERE guild_id = ? AND channel_id = ?
                """, (trigger_message, str(amount_decimal), asset_id, str(interaction.guild.id), str(channel.id)))
                action = "更新"
            else:
                # 新規設定
                await db.execute("""
                    INSERT INTO auto_reward_configs (guild_id, channel_id, trigger_message, reward_amount, asset_id, enabled)
                    VALUES (?, ?, ?, ?, ?, 1)
                """, (str(interaction.guild.id), str(channel.id), trigger_message, str(amount_decimal), asset_id))
                action = "設定"
            
            await db.commit()
        
        embed = create_success_embed(
            f"自動報酬{action}完了",
            f"**{channel.mention}** で「{trigger_message}」と発言したユーザーに **{amount_decimal} {symbol}** を自動付与します。\n\n"
            f"💡 各ユーザーは1回のみ受け取れます。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @autoreward_group.command(name="list", description="自動報酬設定一覧を表示（管理者）")
    async def list(self, interaction: discord.Interaction):
        """自動報酬設定一覧を表示"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            configs = await fetch_all(db, """
                SELECT
                    arc.id,
                    arc.channel_id,
                    arc.trigger_message,
                    arc.reward_amount,
                    a.symbol,
                    arc.enabled,
                    COUNT(DISTINCT arct.user_id) as claim_count
                FROM auto_reward_configs arc
                JOIN assets a ON arc.asset_id = a.id
                LEFT JOIN auto_reward_claims arct ON arc.id = arct.config_id
                WHERE arc.guild_id = ?
                GROUP BY arc.id
                ORDER BY arc.created_at DESC
            """, (str(interaction.guild.id),))
            
            if not configs:
                embed = create_info_embed(
                    "自動報酬設定一覧",
                    "まだ自動報酬が設定されていません。\n`/autoreward setup` で設定してください。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            embed = discord.Embed(
                title="📋 自動報酬設定一覧",
                color=0x3498db,
                timestamp=datetime.now(TZ)
            )
            
            for config_id, channel_id, trigger_msg, reward_amt, symbol, enabled, claim_count in configs:
                channel = interaction.guild.get_channel(int(channel_id))
                channel_name = channel.mention if channel else f"<削除済みチャンネル: {channel_id}>"
                
                status_icon = "✅" if enabled else "🔴"
                status_text = "" if enabled else "\n• 状態: **無効**"
                
                field_value = (
                    f"• トリガー: 「{trigger_msg}」\n"
                    f"• 報酬: **{reward_amt} {symbol}**\n"
                    f"• 受け取り: **{claim_count}人**"
                    f"{status_text}"
                )
                
                embed.add_field(
                    name=f"{status_icon} {channel_name}",
                    value=field_value,
                    inline=False
                )
            
            embed.set_footer(text=f"実行者: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @autoreward_group.command(name="enable", description="自動報酬を有効化（管理者）")
    @app_commands.describe(channel="対象チャンネル")
    async def enable(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """自動報酬を有効化"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            config = await fetch_one(db, """
                SELECT id, enabled FROM auto_reward_configs
                WHERE guild_id = ? AND channel_id = ?
            """, (str(interaction.guild.id), str(channel.id)))
            
            if not config:
                embed = create_error_embed("設定エラー", f"{channel.mention} に自動報酬が設定されていません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            if config[1] == 1:
                embed = create_info_embed("有効化済み", f"{channel.mention} の自動報酬は既に有効化されています。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            await db.execute("""
                UPDATE auto_reward_configs SET enabled = 1
                WHERE guild_id = ? AND channel_id = ?
            """, (str(interaction.guild.id), str(channel.id)))
            await db.commit()
        
        embed = create_success_embed(
            "自動報酬有効化完了",
            f"{channel.mention} の自動報酬を有効化しました。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @autoreward_group.command(name="disable", description="自動報酬を一時無効化（管理者）")
    @app_commands.describe(channel="対象チャンネル")
    async def disable(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """自動報酬を無効化"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            config = await fetch_one(db, """
                SELECT id, enabled FROM auto_reward_configs
                WHERE guild_id = ? AND channel_id = ?
            """, (str(interaction.guild.id), str(channel.id)))
            
            if not config:
                embed = create_error_embed("設定エラー", f"{channel.mention} に自動報酬が設定されていません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            if config[1] == 0:
                embed = create_info_embed("無効化済み", f"{channel.mention} の自動報酬は既に無効化されています。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            await db.execute("""
                UPDATE auto_reward_configs SET enabled = 0
                WHERE guild_id = ? AND channel_id = ?
            """, (str(interaction.guild.id), str(channel.id)))
            await db.commit()
        
        embed = create_success_embed(
            "自動報酬無効化完了",
            f"{channel.mention} の自動報酬を無効化しました。\n`/autoreward enable` で再度有効化できます。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @autoreward_group.command(name="remove", description="自動報酬設定を完全削除（管理者）")
    @app_commands.describe(channel="対象チャンネル")
    async def remove(self, interaction: discord.Interaction, channel: discord.TextChannel):
        """自動報酬設定を削除"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            config = await fetch_one(db, """
                SELECT arc.id, arc.trigger_message, arc.reward_amount, a.symbol, COUNT(DISTINCT arct.user_id) as claim_count
                FROM auto_reward_configs arc
                JOIN assets a ON arc.asset_id = a.id
                LEFT JOIN auto_reward_claims arct ON arc.id = arct.config_id
                WHERE arc.guild_id = ? AND arc.channel_id = ?
                GROUP BY arc.id
            """, (str(interaction.guild.id), str(channel.id)))
            
            if not config:
                embed = create_error_embed("設定エラー", f"{channel.mention} に自動報酬が設定されていません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            config_id, trigger_msg, reward_amt, symbol, claim_count = config
            
            # 削除実行
            await db.execute("""
                DELETE FROM auto_reward_configs
                WHERE guild_id = ? AND channel_id = ?
            """, (str(interaction.guild.id), str(channel.id)))
            await db.commit()
        
        embed = create_success_embed(
            "自動報酬削除完了",
            f"{channel.mention} の自動報酬設定を削除しました。\n\n"
            f"**削除された設定**\n"
            f"• トリガー: 「{trigger_msg}」\n"
            f"• 報酬: {reward_amt} {symbol}\n"
            f"• 受け取り済み: {claim_count}人",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @autoreward_group.command(name="stats", description="自動報酬の統計情報を表示（管理者）")
    @app_commands.describe(channel="対象チャンネル（省略で全体）")
    async def stats(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        """自動報酬の統計情報を表示"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            if channel:
                # 特定チャンネルの統計
                config = await fetch_one(db, """
                    SELECT
                        arc.trigger_message,
                        arc.reward_amount,
                        a.symbol,
                        a.decimals,
                        arc.enabled,
                        arc.created_at,
                        COUNT(DISTINCT arct.user_id) as claim_count,
                        MAX(arct.claimed_at) as last_claim
                    FROM auto_reward_configs arc
                    JOIN assets a ON arc.asset_id = a.id
                    LEFT JOIN auto_reward_claims arct ON arc.id = arct.config_id
                    WHERE arc.guild_id = ? AND arc.channel_id = ?
                    GROUP BY arc.id
                """, (str(interaction.guild.id), str(channel.id)))
                
                if not config:
                    embed = create_error_embed("設定エラー", f"{channel.mention} に自動報酬が設定されていません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                trigger_msg, reward_amt, symbol, decimals, enabled, created_at, claim_count, last_claim = config
                
                total_paid = Decimal(reward_amt) * claim_count
                total_paid_formatted = total_paid.quantize(Decimal(f'1e{-decimals}'))
                
                status_text = "✅ 有効" if enabled else "🔴 無効"
                if last_claim:
                    dt = datetime.fromisoformat(last_claim)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=TZ)
                    last_claim_text = f"<t:{int(dt.timestamp())}:R>"
                else:
                    last_claim_text = "なし"
                
                # 最近の受け取りユーザー
                recent_claims = await fetch_all(db, """
                    SELECT arct.user_id, arct.claimed_at
                    FROM auto_reward_claims arct
                    JOIN auto_reward_configs arc ON arct.config_id = arc.id
                    WHERE arc.guild_id = ? AND arc.channel_id = ?
                    ORDER BY arct.claimed_at DESC
                    LIMIT 5
                """, (str(interaction.guild.id), str(channel.id)))
                
                recent_text = ""
                for i, (user_id, claimed_at) in enumerate(recent_claims, 1):
                    dt = datetime.fromisoformat(claimed_at)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=TZ)
                    timestamp = int(dt.timestamp())
                    recent_text += f"{i}. <@{user_id}> - <t:{timestamp}:R>\n"
                
                if not recent_text:
                    recent_text = "まだ受け取ったユーザーがいません"
                
                embed = discord.Embed(
                    title=f"📊 自動報酬統計 - {channel.name}",
                    color=0x3498db,
                    timestamp=datetime.now(TZ)
                )
                
                embed.add_field(name="トリガーメッセージ", value=f"「{trigger_msg}」", inline=False)
                embed.add_field(name="報酬額", value=f"**{reward_amt} {symbol}**", inline=True)
                embed.add_field(name="状態", value=status_text, inline=True)
                embed.add_field(name="受け取り済みユーザー", value=f"**{claim_count}人**", inline=True)
                embed.add_field(name="総支払額", value=f"**{total_paid_formatted} {symbol}**", inline=True)
                embed.add_field(name="最終受け取り", value=last_claim_text, inline=True)
                
                # 設定日のタイムスタンプ変換
                created_dt = datetime.fromisoformat(created_at)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=TZ)
                embed.add_field(name="設定日", value=f"<t:{int(created_dt.timestamp())}:D>", inline=True)
                embed.add_field(name="最近の受け取りユーザー", value=recent_text, inline=False)
                
            else:
                # 全体統計
                stats = await fetch_all(db, """
                    SELECT
                        arc.channel_id,
                        a.symbol,
                        COUNT(DISTINCT arct.user_id) as claim_count,
                        arc.reward_amount
                    FROM auto_reward_configs arc
                    JOIN assets a ON arc.asset_id = a.id
                    LEFT JOIN auto_reward_claims arct ON arc.id = arct.config_id
                    WHERE arc.guild_id = ?
                    GROUP BY arc.id
                """, (str(interaction.guild.id),))
                
                if not stats:
                    embed = create_info_embed(
                        "統計情報",
                        "まだ自動報酬が設定されていません。",
                        interaction.user
                    )
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                embed = discord.Embed(
                    title="📊 自動報酬統計（全体）",
                    color=0x3498db,
                    timestamp=datetime.now(TZ)
                )
                
                total_configs = len(stats)
                total_users = sum(s[2] for s in stats)
                
                embed.add_field(name="設定数", value=f"**{total_configs}件**", inline=True)
                embed.add_field(name="受け取り延べ人数", value=f"**{total_users}人**", inline=True)
                
                for channel_id, symbol, claim_count, reward_amt in stats:
                    ch = interaction.guild.get_channel(int(channel_id))
                    ch_name = ch.mention if ch else f"<削除済み: {channel_id}>"
                    embed.add_field(
                        name=ch_name,
                        value=f"{claim_count}人 × {reward_amt} {symbol}",
                        inline=True
                    )
            
            embed.set_footer(text=f"実行者: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @autoreward_group.command(name="edit", description="既存設定を編集（管理者）")
    @app_commands.describe(
        channel="編集する設定のチャンネル",
        trigger_message="新しいトリガーメッセージ（省略で変更なし）",
        amount="新しい金額（省略で変更なし）",
        symbol="新しい通貨（省略で変更なし）"
    )
    @app_commands.autocomplete(symbol=currency_autocomplete)
    async def edit(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        trigger_message: str = None,
        amount: str = None,
        symbol: str = None
    ):
        """既存設定を編集"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # 少なくとも1つのパラメータが指定されているかチェック
        if not any([trigger_message, amount, symbol]):
            embed = create_error_embed("入力エラー", "変更する項目を少なくとも1つ指定してください。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 既存設定の確認
            config = await fetch_one(db, """
                SELECT id, trigger_message, reward_amount, asset_id FROM auto_reward_configs
                WHERE guild_id = ? AND channel_id = ?
            """, (str(interaction.guild.id), str(channel.id)))
            
            if not config:
                embed = create_error_embed("設定エラー", f"{channel.mention} に自動報酬が設定されていません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            config_id, current_trigger, current_amount, current_asset_id = config
            
            # 更新する値を決定
            new_trigger = trigger_message if trigger_message else current_trigger
            new_amount = amount if amount else current_amount
            new_asset_id = current_asset_id
            
            # トリガーメッセージのバリデーション
            if trigger_message:
                if not trigger_message.strip():
                    embed = create_error_embed("入力エラー", "トリガーメッセージを入力してください。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                if len(trigger_message) > 500:
                    embed = create_error_embed("入力エラー", "トリガーメッセージは500文字以内で入力してください。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # 金額のバリデーション
            if amount:
                try:
                    if not amount.strip():
                        raise ValueError("Empty input")
                    amount_decimal = to_decimal(amount)
                    if amount_decimal <= 0:
                        raise ValueError("Non-positive value")
                    if not amount_decimal.is_finite():
                        raise ValueError("Not finite")
                    new_amount = str(amount_decimal)
                except ValueError:
                    embed = create_error_embed("入力エラー", "金額は0より大きい有効な数値を入力してください。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # 通貨の確認
            if symbol:
                asset = await get_asset(db, symbol, interaction.guild.id)
                if not asset:
                    embed = create_error_embed("通貨エラー", "指定された通貨が存在しません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                new_asset_id = asset[0]
            
            # 更新実行
            await db.execute("""
                UPDATE auto_reward_configs
                SET trigger_message = ?, reward_amount = ?, asset_id = ?
                WHERE guild_id = ? AND channel_id = ?
            """, (new_trigger, new_amount, new_asset_id, str(interaction.guild.id), str(channel.id)))
            await db.commit()
            
            # 通貨シンボル取得
            asset_info = await get_asset_info_by_id(db, new_asset_id)
            new_symbol = asset_info[0] if asset_info else "COIN"
        
        changes = []
        if trigger_message:
            changes.append(f"• トリガー: 「{new_trigger}」")
        if amount:
            changes.append(f"• 報酬: {new_amount} {new_symbol}")
        if symbol:
            changes.append(f"• 通貨: {new_symbol}")
        
        embed = create_success_embed(
            "自動報酬編集完了",
            f"{channel.mention} の自動報酬設定を更新しました。\n\n**変更内容**\n" + "\n".join(changes),
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """メッセージ受信時の処理（自動報酬トリガー）"""
        # Botのメッセージは無視
        if message.author.bot:
            return
        
        # DMは無視
        if not message.guild:
            return
        
        # 自動報酬設定をチェック
        async with aiosqlite.connect(DB_PATH) as db:
            config = await fetch_one(db, """
                SELECT id, trigger_message, reward_amount, asset_id, enabled
                FROM auto_reward_configs
                WHERE guild_id = ? AND channel_id = ? AND enabled = 1
            """, (str(message.guild.id), str(message.channel.id)))
            
            if not config:
                return
            
            config_id, trigger_message, reward_amount, asset_id, enabled = config
            
            # トリガーメッセージと一致するかチェック
            if message.content.strip() != trigger_message:
                return
            
            # ユーザーが既に受け取っているかチェック
            uid = await upsert_user(db, message.author.id)
            already_claimed = await fetch_one(db, """
                SELECT id FROM auto_reward_claims
                WHERE config_id = ? AND user_id = ?
            """, (config_id, uid))
            
            if already_claimed:
                # 既に受け取っている場合は❌リアクション
                try:
                    await message.add_reaction("❌")
                except:
                    pass
                return
            
            # 通貨情報を取得
            asset_info = await get_asset_info_by_id(db, asset_id)
            if not asset_info:
                return
            
            symbol, decimals = asset_info
            reward_decimal = Decimal(reward_amount)
            
            # Treasuryアカウントを取得
            treasury_acc = await account_id_by_name(db, "treasury", message.guild.id)
            
            # Treasury残高を確認・補充
            await auto_refill_treasury_if_needed(db, treasury_acc, asset_id, message.guild.id, reward_decimal)
            
            # ユーザーアカウントを取得
            user_acc = await ensure_user_account(db, message.author.id, message.guild.id)
            
            # 報酬を付与
            tx_id = await new_transaction(db, kind="auto_reward", created_by_user_id=uid, unique_hash=None, reference=f"Auto reward trigger: {trigger_message}")
            await post_ledger(db, tx_id, treasury_acc, asset_id, -reward_decimal)
            await post_ledger(db, tx_id, user_acc, asset_id, reward_decimal)
            
            # 受取記録を保存
            await db.execute("""
                INSERT INTO auto_reward_claims(config_id, user_id, guild_id)
                VALUES (?, ?, ?)
            """, (config_id, uid, str(message.guild.id)))
            
            await db.commit()
            
            # ✅リアクションを追加
            try:
                await message.add_reaction("✅")
            except:
                pass
            
            # 引用返信で通知
            try:
                await message.reply(
                    f"**{reward_decimal} {symbol}** を発行しました！",
                    mention_author=False
                )
            except:
                pass


async def setup(bot: commands.Bot):
    """Cogをセットアップ"""
    await bot.add_cog(AutoRewardCog(bot))
