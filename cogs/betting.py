"""
Betting System Cog

ユーザーが選手に通貨を賭けて、勝者に賞金を分配するシステム
"""

import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from datetime import datetime
from typing import Optional, List, Tuple
import math

from config import DB_PATH
from database import fetch_one, fetch_all, execute_query


class BettingSystem(commands.Cog):
    """投票/賭けシステム"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    async def cog_load(self):
        """Cog読み込み時にテーブルを作成"""
        async with aiosqlite.connect(DB_PATH) as db:
            # 投票イベントテーブル
            await db.execute("""
                CREATE TABLE IF NOT EXISTS betting_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    event_name TEXT NOT NULL,
                    currency_symbol TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closed_at TEXT,
                    winner_user_id INTEGER,
                    is_active INTEGER DEFAULT 1,
                    total_pool INTEGER DEFAULT 0
                )
            """)
            
            # 選手登録テーブル
            await db.execute("""
                CREATE TABLE IF NOT EXISTS betting_players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    added_at TEXT NOT NULL,
                    FOREIGN KEY (event_id) REFERENCES betting_events(id),
                    UNIQUE(event_id, user_id)
                )
            """)
            
            # 賭けテーブル
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    target_user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    placed_at TEXT NOT NULL,
                    FOREIGN KEY (event_id) REFERENCES betting_events(id)
                )
            """)
            
            # インデックス作成
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_betting_events_guild 
                ON betting_events(guild_id, is_active)
            """)
            
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_betting_players_event
                ON betting_players(event_id)
            """)
            
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_bets_event 
                ON bets(event_id, target_user_id)
            """)
            
            await db.commit()
            print("[BETTING] テーブルを初期化しました")
    
    def calculate_odds(self, target_total: int, event_total: int) -> float:
        """オッズを計算（人気度に応じて変動）"""
        if target_total == 0 or event_total == 0:
            return 2.0  # デフォルトオッズ
        
        # オッズ = 総賭け額 / 対象への賭け額
        odds = event_total / target_total
        
        # 最小オッズを1.1倍に設定
        return max(1.1, round(odds, 2))
    
    async def get_target_totals(self, db: aiosqlite.Connection, event_id: int) -> List[Tuple[int, int]]:
        """各対象への賭け総額を取得"""
        result = await fetch_all(db, """
            SELECT target_user_id, SUM(amount) as total
            FROM bets
            WHERE event_id = ?
            GROUP BY target_user_id
        """, (event_id,))
        return result
    
    @app_commands.command(name="bet_create", description="新しい賭けイベントを作成")
    @app_commands.describe(
        event_name="イベント名（例: 大会決勝戦）",
        currency_symbol="使用する通貨記号"
    )
    @app_commands.default_permissions(manage_guild=True)
    async def bet_create(
        self,
        interaction: discord.Interaction,
        event_name: str,
        currency_symbol: str
    ):
        """賭けイベントを作成"""
        await interaction.response.defer(ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 通貨の存在確認
            currency = await fetch_one(db, """
                SELECT id FROM currencies 
                WHERE guild_id = ? AND symbol = ?
            """, (interaction.guild_id, currency_symbol))
            
            if not currency:
                await interaction.followup.send(
                    f"❌ 通貨記号 `{currency_symbol}` が見つかりません。",
                    ephemeral=True
                )
                return
            
            # 既存のアクティブイベント確認
            existing = await fetch_one(db, """
                SELECT id FROM betting_events
                WHERE guild_id = ? AND is_active = 1
            """, (interaction.guild_id,))
            
            if existing:
                await interaction.followup.send(
                    "❌ 既にアクティブな賭けイベントが存在します。\n"
                    "`/bet_close` で終了してから新規作成してください。",
                    ephemeral=True
                )
                return
            
            # イベント作成
            await execute_query(db, """
                INSERT INTO betting_events (guild_id, event_name, currency_symbol, created_at)
                VALUES (?, ?, ?, ?)
            """, (interaction.guild_id, event_name, currency_symbol, datetime.now().isoformat()))
            
            await db.commit()
        
        embed = discord.Embed(
            title="賭けイベント作成",
            description=f"イベント「**{event_name}**」を作成しました。",
            color=0x2ecc71
        )
        embed.add_field(name="使用通貨", value=currency_symbol, inline=True)
        embed.add_field(name="状態", value="受付中", inline=True)
        embed.set_footer(text="ユーザーは /bet コマンドで賭けることができます")
        
        await interaction.followup.send(embed=embed)
    
    @app_commands.command(name="bet_player_add", description="賭けイベントに選手を追加")
    @app_commands.describe(player="追加する選手")
    @app_commands.default_permissions(manage_guild=True)
    async def bet_player_add(
        self,
        interaction: discord.Interaction,
        player: discord.Member
    ):
        """選手を追加"""
        await interaction.response.defer(ephemeral=True)
        
        if player.bot:
            await interaction.followup.send("❌ Botは選手として登録できません。", ephemeral=True)
            return
        
        async with aiosqlite.connect(DB_PATH) as db:
            # アクティブなイベント取得
            event = await fetch_one(db, """
                SELECT id, event_name FROM betting_events
                WHERE guild_id = ? AND is_active = 1
            """, (interaction.guild_id,))
            
            if not event:
                await interaction.followup.send(
                    "❌ 現在受付中の賭けイベントがありません。",
                    ephemeral=True
                )
                return
            
            event_id, event_name = event
            
            # 既に登録されているか確認
            existing = await fetch_one(db, """
                SELECT id FROM betting_players
                WHERE event_id = ? AND user_id = ?
            """, (event_id, player.id))
            
            if existing:
                await interaction.followup.send(
                    f"❌ {player.mention} は既に選手として登録されています。",
                    ephemeral=True
                )
                return
            
            # 選手を追加
            await execute_query(db, """
                INSERT INTO betting_players (event_id, user_id, added_at)
                VALUES (?, ?, ?)
            """, (event_id, player.id, datetime.now().isoformat()))
            
            await db.commit()
        
        embed = discord.Embed(
            title="選手登録",
            description=f"{player.mention} を選手として登録しました。",
            color=0x2ecc71
        )
        embed.add_field(name="イベント", value=event_name, inline=True)
        embed.set_footer(text="ユーザーはこの選手に賭けることができます")
        
        await interaction.followup.send(embed=embed)
    
    @app_commands.command(name="bet_player_remove", description="賭けイベントから選手を削除")
    @app_commands.describe(player="削除する選手")
    @app_commands.default_permissions(manage_guild=True)
    async def bet_player_remove(
        self,
        interaction: discord.Interaction,
        player: discord.Member
    ):
        """選手を削除（既に賭けがある場合は削除不可）"""
        await interaction.response.defer(ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # アクティブなイベント取得
            event = await fetch_one(db, """
                SELECT id, event_name FROM betting_events
                WHERE guild_id = ? AND is_active = 1
            """, (interaction.guild_id,))
            
            if not event:
                await interaction.followup.send(
                    "❌ 現在受付中の賭けイベントがありません。",
                    ephemeral=True
                )
                return
            
            event_id, event_name = event
            
            # 選手が登録されているか確認
            existing = await fetch_one(db, """
                SELECT id FROM betting_players
                WHERE event_id = ? AND user_id = ?
            """, (event_id, player.id))
            
            if not existing:
                await interaction.followup.send(
                    f"❌ {player.mention} は選手として登録されていません。",
                    ephemeral=True
                )
                return
            
            # 既に賭けがあるか確認
            bets = await fetch_one(db, """
                SELECT COUNT(*) FROM bets
                WHERE event_id = ? AND target_user_id = ?
            """, (event_id, player.id))
            
            if bets and bets[0] > 0:
                await interaction.followup.send(
                    f"❌ {player.mention} には既に {bets[0]} 件の賭けがあるため削除できません。",
                    ephemeral=True
                )
                return
            
            # 選手を削除
            await execute_query(db, """
                DELETE FROM betting_players
                WHERE event_id = ? AND user_id = ?
            """, (event_id, player.id))
            
            await db.commit()
        
        embed = discord.Embed(
            title="選手削除",
            description=f"{player.mention} を選手リストから削除しました。",
            color=0xe74c3c
        )
        embed.add_field(name="イベント", value=event_name, inline=True)
        
        await interaction.followup.send(embed=embed)
    
    @app_commands.command(name="bet_players", description="登録されている選手一覧を表示")
    async def bet_players(self, interaction: discord.Interaction):
        """選手一覧を表示"""
        await interaction.response.defer()
        
        async with aiosqlite.connect(DB_PATH) as db:
            # アクティブなイベント取得
            event = await fetch_one(db, """
                SELECT id, event_name, currency_symbol, total_pool
                FROM betting_events
                WHERE guild_id = ? AND is_active = 1
            """, (interaction.guild_id,))
            
            if not event:
                await interaction.followup.send("❌ 現在受付中の賭けイベントがありません。")
                return
            
            event_id, event_name, currency_symbol, total_pool = event
            
            # 登録されている選手を取得
            players = await fetch_all(db, """
                SELECT user_id FROM betting_players
                WHERE event_id = ?
                ORDER BY added_at
            """, (event_id,))
            
            if not players:
                await interaction.followup.send("まだ選手が登録されていません。")
                return
            
            # 各選手への賭け状況を取得
            target_totals_dict = {}
            target_totals = await self.get_target_totals(db, event_id)
            for tid, ttotal in target_totals:
                target_totals_dict[tid] = ttotal
        
        embed = discord.Embed(
            title=f"{event_name} - 選手一覧",
            description=f"総賭け額: **{total_pool}{currency_symbol}**\n登録選手数: {len(players)}人",
            color=0x3498db
        )
        
        for (player_id,) in players:
            user = interaction.guild.get_member(player_id)
            if not user:
                continue
            
            target_total = target_totals_dict.get(player_id, 0)
            
            if target_total > 0:
                odds = self.calculate_odds(target_total, total_pool)
                popularity = (target_total / total_pool * 100) if total_pool > 0 else 0
                
                embed.add_field(
                    name=f"{user.display_name}",
                    value=f"賭け額: {target_total}{currency_symbol}\n"
                          f"人気度: {popularity:.1f}%\n"
                          f"オッズ: **{odds}倍**",
                    inline=True
                )
            else:
                embed.add_field(
                    name=f"{user.display_name}",
                    value="賭けなし",
                    inline=True
                )
        
        await interaction.followup.send(embed=embed)
    
    @app_commands.command(name="bet", description="選手に賭ける")
    @app_commands.describe(
        target="賭ける対象のユーザー",
        amount="賭ける金額"
    )
    async def bet(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        amount: int
    ):
        """選手に賭ける"""
        await interaction.response.defer(ephemeral=True)
        
        if amount <= 0:
            await interaction.followup.send("❌ 1以上の金額を指定してください。", ephemeral=True)
            return
        
        if target.bot:
            await interaction.followup.send("❌ Botには賭けられません。", ephemeral=True)
            return
        
        async with aiosqlite.connect(DB_PATH) as db:
            # アクティブなイベント取得
            event = await fetch_one(db, """
                SELECT id, event_name, currency_symbol, total_pool
                FROM betting_events
                WHERE guild_id = ? AND is_active = 1
            """, (interaction.guild_id,))
            
            if not event:
                await interaction.followup.send(
                    "❌ 現在受付中の賭けイベントがありません。",
                    ephemeral=True
                )
                return
            
            event_id, event_name, currency_symbol, total_pool = event
            
            # 対象が選手として登録されているか確認
            is_player = await fetch_one(db, """
                SELECT id FROM betting_players
                WHERE event_id = ? AND user_id = ?
            """, (event_id, target.id))
            
            if not is_player:
                await interaction.followup.send(
                    f"❌ {target.mention} は選手として登録されていません。\n"
                    "`/bet_players` で登録済み選手を確認してください。",
                    ephemeral=True
                )
                return
            
            # ユーザーの残高確認
            balance = await fetch_one(db, """
                SELECT amount FROM balances
                WHERE guild_id = ? AND user_id = ? AND currency_symbol = ?
            """, (interaction.guild_id, interaction.user.id, currency_symbol))
            
            current_balance = balance[0] if balance else 0
            
            if current_balance < amount:
                await interaction.followup.send(
                    f"❌ 残高が不足しています。\n"
                    f"現在の残高: {current_balance}{currency_symbol}",
                    ephemeral=True
                )
                return
            
            # 既存の賭けを確認
            existing_bet = await fetch_one(db, """
                SELECT SUM(amount) FROM bets
                WHERE event_id = ? AND user_id = ?
            """, (event_id, interaction.user.id))
            
            existing_amount = existing_bet[0] if existing_bet and existing_bet[0] else 0
            
            # 残高から引き落とし
            await execute_query(db, """
                INSERT INTO balances (guild_id, user_id, currency_symbol, amount)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, currency_symbol) 
                DO UPDATE SET amount = amount - ?
            """, (interaction.guild_id, interaction.user.id, currency_symbol, 0, amount))
            
            # 賭けを記録
            await execute_query(db, """
                INSERT INTO bets (event_id, user_id, target_user_id, amount, placed_at)
                VALUES (?, ?, ?, ?, ?)
            """, (event_id, interaction.user.id, target.id, amount, datetime.now().isoformat()))
            
            # 総賭け額を更新
            await execute_query(db, """
                UPDATE betting_events
                SET total_pool = total_pool + ?
                WHERE id = ?
            """, (amount, event_id))
            
            await db.commit()
            
            # 現在のオッズを計算
            target_totals = await self.get_target_totals(db, event_id)
            new_total_pool = total_pool + amount
            
            target_total = 0
            for tid, ttotal in target_totals:
                if tid == target.id:
                    target_total = ttotal
                    break
            
            odds = self.calculate_odds(target_total, new_total_pool)
        
        embed = discord.Embed(
            title="賭けを受付",
            description=f"「**{event_name}**」への賭けを受け付けました。",
            color=0x3498db
        )
        embed.add_field(name="賭け先", value=target.mention, inline=True)
        embed.add_field(name="金額", value=f"{amount}{currency_symbol}", inline=True)
        embed.add_field(name="現在のオッズ", value=f"{odds}倍", inline=True)
        embed.add_field(name="予想配当", value=f"{int(amount * odds)}{currency_symbol}", inline=False)
        
        if existing_amount > 0:
            embed.set_footer(text=f"このイベントへの累計賭け額: {existing_amount + amount}{currency_symbol}")
        
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    @app_commands.command(name="bet_odds", description="現在のオッズを確認")
    async def bet_odds(self, interaction: discord.Interaction):
        """現在のオッズを表示"""
        await interaction.response.defer()
        
        async with aiosqlite.connect(DB_PATH) as db:
            # アクティブなイベント取得
            event = await fetch_one(db, """
                SELECT id, event_name, currency_symbol, total_pool
                FROM betting_events
                WHERE guild_id = ? AND is_active = 1
            """, (interaction.guild_id,))
            
            if not event:
                await interaction.followup.send("❌ 現在受付中の賭けイベントがありません。")
                return
            
            event_id, event_name, currency_symbol, total_pool = event
            
            # 各対象への賭け状況取得
            target_totals = await self.get_target_totals(db, event_id)
            
            if not target_totals:
                await interaction.followup.send("まだ誰も賭けていません。")
                return
        
        embed = discord.Embed(
            title=f"{event_name}",
            description=f"総賭け額: **{total_pool}{currency_symbol}**",
            color=0xf39c12
        )
        
        # オッズ順にソート
        sorted_targets = sorted(
            target_totals,
            key=lambda x: x[1],
            reverse=True
        )
        
        for target_id, target_total in sorted_targets:
            user = interaction.guild.get_member(target_id)
            if not user:
                continue
            
            odds = self.calculate_odds(target_total, total_pool)
            popularity = (target_total / total_pool * 100) if total_pool > 0 else 0
            
            embed.add_field(
                name=f"{user.display_name}",
                value=f"賭け額: {target_total}{currency_symbol}\n"
                      f"人気度: {popularity:.1f}%\n"
                      f"オッズ: **{odds}倍**",
                inline=True
            )
        
        await interaction.followup.send(embed=embed)
    
    @app_commands.command(name="bet_finish", description="賭けイベントを終了し、勝者を指定")
    @app_commands.describe(winner="勝者のユーザー")
    @app_commands.default_permissions(manage_guild=True)
    async def bet_finish(
        self,
        interaction: discord.Interaction,
        winner: discord.Member
    ):
        """賭けイベントを終了し、配当を分配"""
        await interaction.response.defer()
        
        if winner.bot:
            await interaction.followup.send("❌ Botを勝者に指定できません。")
            return
        
        async with aiosqlite.connect(DB_PATH) as db:
            # アクティブなイベント取得
            event = await fetch_one(db, """
                SELECT id, event_name, currency_symbol, total_pool
                FROM betting_events
                WHERE guild_id = ? AND is_active = 1
            """, (interaction.guild_id,))
            
            if not event:
                await interaction.followup.send("❌ 現在受付中の賭けイベントがありません。")
                return
            
            event_id, event_name, currency_symbol, total_pool = event
            
            # 勝者が選手として登録されているか確認
            is_player = await fetch_one(db, """
                SELECT id FROM betting_players
                WHERE event_id = ? AND user_id = ?
            """, (event_id, winner.id))
            
            if not is_player:
                await interaction.followup.send(
                    f"❌ {winner.mention} は選手として登録されていません。"
                )
                return
            
            # 勝者への賭けを確認
            winner_bets = await fetch_all(db, """
                SELECT user_id, amount FROM bets
                WHERE event_id = ? AND target_user_id = ?
            """, (event_id, winner.id))
            
            if not winner_bets:
                await interaction.followup.send(
                    f"❌ {winner.mention} に賭けたユーザーがいません。"
                )
                return
            
            # 勝者への総賭け額
            winner_total = sum(amount for _, amount in winner_bets)
            
            # オッズ計算
            odds = self.calculate_odds(winner_total, total_pool)
            
            # 配当を計算して分配
            payouts = []
            for user_id, bet_amount in winner_bets:
                payout = int(bet_amount * odds)
                
                # 残高に加算
                await execute_query(db, """
                    INSERT INTO balances (guild_id, user_id, currency_symbol, amount)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id, currency_symbol)
                    DO UPDATE SET amount = amount + ?
                """, (interaction.guild_id, user_id, currency_symbol, payout, payout))
                
                payouts.append((user_id, bet_amount, payout))
            
            # イベントをクローズ
            await execute_query(db, """
                UPDATE betting_events
                SET is_active = 0, closed_at = ?, winner_user_id = ?
                WHERE id = ?
            """, (datetime.now().isoformat(), winner.id, event_id))
            
            await db.commit()
        
        # 結果を表示
        embed = discord.Embed(
            title="賭けイベント終了",
            description=f"「**{event_name}**」が終了しました。",
            color=0xe74c3c
        )
        embed.add_field(name="勝者", value=winner.mention, inline=True)
        embed.add_field(name="確定オッズ", value=f"{odds}倍", inline=True)
        embed.add_field(name="総賭け額", value=f"{total_pool}{currency_symbol}", inline=True)
        
        # 配当一覧
        payout_text = ""
        for user_id, bet_amount, payout in payouts[:10]:  # 最大10人まで表示
            user = interaction.guild.get_member(user_id)
            if user:
                profit = payout - bet_amount
                payout_text += f"{user.mention}: {bet_amount}{currency_symbol} → **{payout}{currency_symbol}** (+{profit})\n"
        
        if len(payouts) > 10:
            payout_text += f"\n...他 {len(payouts) - 10} 人"
        
        if payout_text:
            embed.add_field(name="配当一覧", value=payout_text, inline=False)
        
        embed.set_footer(text=f"当選者数: {len(payouts)}人")
        
        await interaction.followup.send(embed=embed)
    
    @app_commands.command(name="bet_cancel", description="賭けイベントをキャンセル（賭け金を返金）")
    @app_commands.default_permissions(manage_guild=True)
    async def bet_cancel(self, interaction: discord.Interaction):
        """賭けイベントをキャンセルし、全員に返金"""
        await interaction.response.defer()
        
        async with aiosqlite.connect(DB_PATH) as db:
            # アクティブなイベント取得
            event = await fetch_one(db, """
                SELECT id, event_name, currency_symbol
                FROM betting_events
                WHERE guild_id = ? AND is_active = 1
            """, (interaction.guild_id,))
            
            if not event:
                await interaction.followup.send("❌ 現在受付中の賭けイベントがありません。")
                return
            
            event_id, event_name, currency_symbol = event
            
            # 全ての賭けを取得
            all_bets = await fetch_all(db, """
                SELECT user_id, amount FROM bets
                WHERE event_id = ?
            """, (event_id,))
            
            if not all_bets:
                # 賭けがない場合はそのまま終了
                await execute_query(db, """
                    UPDATE betting_events
                    SET is_active = 0, closed_at = ?
                    WHERE id = ?
                """, (datetime.now().isoformat(), event_id))
                await db.commit()
                
                await interaction.followup.send(
                    f"イベント「**{event_name}**」をキャンセルしました。\n"
                    "（賭けがなかったため返金はありません）"
                )
                return
            
            # 返金処理
            refund_count = 0
            for user_id, amount in all_bets:
                await execute_query(db, """
                    INSERT INTO balances (guild_id, user_id, currency_symbol, amount)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id, currency_symbol)
                    DO UPDATE SET amount = amount + ?
                """, (interaction.guild_id, user_id, currency_symbol, amount, amount))
                refund_count += 1
            
            # イベントをクローズ
            await execute_query(db, """
                UPDATE betting_events
                SET is_active = 0, closed_at = ?
                WHERE id = ?
            """, (datetime.now().isoformat(), event_id))
            
            await db.commit()
        
        embed = discord.Embed(
            title="賭けイベントキャンセル",
            description=f"「**{event_name}**」をキャンセルしました。",
            color=0x95a5a6
        )
        embed.add_field(name="返金件数", value=f"{refund_count}件", inline=True)
        embed.set_footer(text="全ての賭け金を返金しました")
        
        await interaction.followup.send(embed=embed)
    
    @app_commands.command(name="bet_history", description="過去の賭けイベント履歴を表示")
    async def bet_history(self, interaction: discord.Interaction):
        """過去の賭けイベント履歴"""
        await interaction.response.defer(ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            events = await fetch_all(db, """
                SELECT event_name, currency_symbol, total_pool, winner_user_id, closed_at
                FROM betting_events
                WHERE guild_id = ? AND is_active = 0
                ORDER BY closed_at DESC
                LIMIT 10
            """, (interaction.guild_id,))
            
            if not events:
                await interaction.followup.send("まだ終了した賭けイベントがありません。", ephemeral=True)
                return
        
        embed = discord.Embed(
            title="賭けイベント履歴",
            description="過去10件のイベント",
            color=0x9b59b6
        )
        
        for event_name, currency_symbol, total_pool, winner_id, closed_at in events:
            winner = interaction.guild.get_member(winner_id) if winner_id else None
            winner_text = winner.mention if winner else "キャンセル"
            
            embed.add_field(
                name=event_name,
                value=f"総額: {total_pool}{currency_symbol}\n"
                      f"勝者: {winner_text}\n"
                      f"終了: {closed_at[:10]}",
                inline=False
            )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    @app_commands.command(name="bet_mybets", description="自分の賭け状況を確認")
    async def bet_mybets(self, interaction: discord.Interaction):
        """自分の賭け状況を確認"""
        await interaction.response.defer(ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # アクティブなイベント取得
            event = await fetch_one(db, """
                SELECT id, event_name, currency_symbol, total_pool
                FROM betting_events
                WHERE guild_id = ? AND is_active = 1
            """, (interaction.guild_id,))
            
            if not event:
                await interaction.followup.send("❌ 現在受付中の賭けイベントがありません。", ephemeral=True)
                return
            
            event_id, event_name, currency_symbol, total_pool = event
            
            # 自分の賭けを取得
            my_bets = await fetch_all(db, """
                SELECT target_user_id, amount
                FROM bets
                WHERE event_id = ? AND user_id = ?
            """, (event_id, interaction.user.id))
            
            if not my_bets:
                await interaction.followup.send(
                    f"「**{event_name}**」にまだ賭けていません。",
                    ephemeral=True
                )
                return
            
            # 各対象への賭け総額を取得してオッズ計算
            target_totals = await self.get_target_totals(db, event_id)
        
        embed = discord.Embed(
            title=f"あなたの賭け状況",
            description=f"イベント: **{event_name}**",
            color=0x3498db
        )
        
        total_bet = 0
        total_potential = 0
        
        for target_id, amount in my_bets:
            user = interaction.guild.get_member(target_id)
            if not user:
                continue
            
            # このターゲットへの総賭け額を取得
            target_total = 0
            for tid, ttotal in target_totals:
                if tid == target_id:
                    target_total = ttotal
                    break
            
            odds = self.calculate_odds(target_total, total_pool)
            potential = int(amount * odds)
            
            total_bet += amount
            total_potential += potential
            
            embed.add_field(
                name=user.display_name,
                value=f"賭け額: {amount}{currency_symbol}\n"
                      f"オッズ: {odds}倍\n"
                      f"予想配当: {potential}{currency_symbol}",
                inline=True
            )
        
        embed.set_footer(
            text=f"合計賭け額: {total_bet}{currency_symbol} | "
                 f"最大予想配当: {total_potential}{currency_symbol}"
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    """Cogをセットアップ"""
    await bot.add_cog(BettingSystem(bot))