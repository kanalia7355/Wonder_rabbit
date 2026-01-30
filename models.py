"""
データモデル・Viewモジュール

Discord UIコンポーネント（View、Button、Modal等）を定義します。
"""

import discord
import aiosqlite
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from config import TZ, DB_PATH
from database import (
    fetch_one, fetch_all, upsert_user, ensure_user_account,
    balance_of, account_id_by_name, auto_refill_treasury_if_needed,
    new_transaction, post_ledger, get_asset_info_by_id
)
from embeds import create_error_embed, create_success_embed, create_info_embed


# ==================== 通貨削除確認View ====================

class CurrencyDeleteConfirmView(discord.ui.View):
    """
    通貨削除の確認View
    """
    
    def __init__(self, symbol: str, guild_id: int, user: discord.User, asset_info: tuple, balance_info: list, claim_count: int):
        super().__init__(timeout=300)  # 5分でタイムアウト
        self.symbol = symbol
        self.guild_id = guild_id
        self.user = user
        self.asset_info = asset_info
        self.balance_info = balance_info
        self.claim_count = claim_count
    
    @discord.ui.button(label="🗑️ 削除を実行", style=discord.ButtonStyle.danger)
    async def confirm_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            embed = create_error_embed("権限エラー", "この操作を実行できるのは元のコマンド実行者のみです。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        await self._execute_deletion(interaction)
    
    @discord.ui.button(label="❌ キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            embed = create_error_embed("権限エラー", "この操作を実行できるのは元のコマンド実行者のみです。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        embed = create_success_embed("削除キャンセル", f"通貨 **{self.symbol}** の削除をキャンセルしました。", interaction.user)
        await interaction.response.edit_message(embed=embed, view=None)
    
    async def _execute_deletion(self, interaction: discord.Interaction):
        """実際の削除処理を実行"""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("BEGIN"):
                asset_id, sym, asset_name, decimals = self.asset_info
                
                try:
                    # 関連データを全て削除
                    # 1. 仕訳帳エントリ
                    await db.execute("DELETE FROM ledger_entries WHERE asset_id = ?", (asset_id,))
                    
                    # 2. 請求
                    await db.execute("DELETE FROM claims WHERE asset_id = ?",(asset_id,))
                    
                    # 3. デイリー報酬
                    await db.execute("DELETE FROM daily_role_rewards WHERE asset_id = ?", (asset_id,))
                    await db.execute("DELETE FROM daily_log WHERE asset_id = ?", (asset_id,))
                    
                    # 4. 自動報酬（メッセージIDベース）
                    await db.execute("DELETE FROM autorewards WHERE asset_id = ?", (asset_id,))
                    
                    # 5. 自動報酬（メッセージトリガーベース）
                    await db.execute("DELETE FROM auto_reward_configs WHERE asset_id = ?", (asset_id,))
                    
                    # 6. ロールプラン（スキーマが変更されたため削除不要）
                    # role_plansテーブルにはasset_idカラムがないため、スキップ
                    
                    # 7. 最後に通貨自体を削除
                    await db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
                    
                    await db.commit()
                    
                    # 削除完了メッセージ
                    description = f"**{self.symbol}** ({asset_name}) を完全に削除しました。\n"
                    if self.balance_info:
                        description += f"\n**削除されたデータ:**\n"
                        description += f"• 残高レコード: {len(self.balance_info)}件\n"
                    if self.claim_count > 0:
                        description += f"• 請求レコード: {self.claim_count}件\n"
                    
                    embed = create_success_embed("通貨削除完了", description, interaction.user)
                    await interaction.response.edit_message(embed=embed, view=None)
                    
                except Exception as e:
                    embed = create_error_embed("削除エラー", f"削除中にエラーが発生しました: {str(e)}", interaction.user)
                    await interaction.response.edit_message(embed=embed, view=None)


# ==================== ロール購入パネルView ====================

class RolePurchaseView(discord.ui.View):
    """
    ロール購入パネルのView（永続的）
    """
    
    def __init__(self, panel_id: int):
        super().__init__(timeout=None)  # 永続化
        self.panel_id = panel_id
    
    @discord.ui.button(
        label="🛒 購入",
        style=discord.ButtonStyle.primary,
        custom_id="role_purchase_button"
    )
    async def purchase_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        """ロール購入ボタンのコールバック"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このボタンはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # プラン一覧を取得
        async with aiosqlite.connect(DB_PATH) as db:
            plans = await fetch_all(db, """
                SELECT id, plan_name, price, currency_symbol, duration_hours
                FROM role_plans
                WHERE panel_id = ?
                ORDER BY price
            """, (self.panel_id,))
            
            if not plans:
                embed = create_error_embed("プランエラー", "このパネルにはプランがありません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # プラン選択用のViewを作成して表示
        view = RolePlanSelectView(self.panel_id, plans)
        embed = create_info_embed(
            "プラン選択",
            "購入するプランを選択してください。",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class RolePlanSelectView(discord.ui.View):
    """プラン選択用のView（一時的）"""
    
    def __init__(self, panel_id: int, plans: list):
        super().__init__(timeout=300)  # 5分でタイムアウト
        self.panel_id = panel_id
        
        # ドロップダウンを追加
        self.add_item(RolePlanSelectDropdown(panel_id, plans))


class RolePlanSelectDropdown(discord.ui.Select):
    """プラン選択用のドロップダウン"""
    
    def __init__(self, panel_id: int, plans: list):
        self.panel_id = panel_id
        
        # プランの選択肢を作成
        options = []
        for plan_id, plan_name, price, currency_symbol, duration_hours in plans:
            # 期限表示を整形
            hours_text = f"{duration_hours}時間"
            if duration_hours >= 24:
                days = duration_hours // 24
                remaining_hours = duration_hours % 24
                hours_text = f"{days}日" + (f"{remaining_hours}時間" if remaining_hours > 0 else "")
            
            options.append(discord.SelectOption(
                label=f"{plan_name}",
                description=f"{price} {currency_symbol} - {hours_text}",
                value=str(plan_id)
            ))
        
        super().__init__(
            placeholder="プランを選択してください",
            options=options,
            min_values=1,
            max_values=1
        )
    
    async def callback(self, interaction: discord.Interaction):
        """ドロップダウン選択時のコールバック"""
        plan_id = int(self.values[0])
        
        # プラン情報を取得して購入処理を実行
        async with aiosqlite.connect(DB_PATH) as db:
            # プラン情報を取得
            plan = await fetch_one(db, """
                SELECT rp.id, rp.plan_name, rp.role_id, rp.price, rp.currency_symbol, rp.duration_hours, rp.guild_id
                FROM role_plans rp
                WHERE rp.id = ? AND rp.panel_id = ?
            """, (plan_id, self.panel_id))
            
            if not plan:
                embed = create_error_embed("プランエラー", "指定されたプランが見つかりません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            plan_id, plan_name, role_id, price, currency_symbol, duration_hours, guild_id = plan
            price_decimal = Decimal(price)
            
            # 通貨情報を取得
            from database import get_asset
            asset = await get_asset(db, currency_symbol, interaction.guild.id)
            if not asset:
                embed = create_error_embed("通貨エラー", f"通貨 **{currency_symbol}** が見つかりません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            asset_id, symbol, asset_name, decimals = asset
            
            # ユーザーの残高を確認
            uid = await upsert_user(db, interaction.user.id)
            user_acc = await ensure_user_account(db, interaction.user.id, interaction.guild.id)
            user_balance = await balance_of(db, user_acc, asset_id)
            
            if user_balance < price_decimal:
                embed = create_error_embed(
                    "残高不足",
                    f"残高が不足しています。\n必要: {price_decimal} {symbol}\n現在: {user_balance} {symbol}",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # Treasuryアカウントを取得
            treasury_acc = await account_id_by_name(db, "treasury", interaction.guild.id)
            
            # 支払い処理
            tx_id = await new_transaction(db, kind="role_purchase", created_by_user_id=uid, unique_hash=None, reference=f"Role purchase: {role_id}")
            await post_ledger(db, tx_id, user_acc, asset_id, -price_decimal)
            await post_ledger(db, tx_id, treasury_acc, asset_id, price_decimal)
            
            # 購入記録を保存
            expires_at = datetime.now(TZ) + timedelta(hours=duration_hours)
            await db.execute("""
                INSERT INTO role_purchases(user_id, plan_id, guild_id, expires_at)
                VALUES (?, ?, ?, ?)
            """, (uid, plan_id, str(interaction.guild.id), expires_at.isoformat()))
            
            await db.commit()
            
            # ロールを付与
            role = interaction.guild.get_role(int(role_id))
            if role:
                await interaction.user.add_roles(role)
                
                # 期限表示を整形
                hours_text = f"{duration_hours}時間"
                if duration_hours >= 24:
                    days = duration_hours // 24
                    remaining_hours = duration_hours % 24
                    hours_text = f"{days}日" + (f"{remaining_hours}時間" if remaining_hours > 0 else "")
                
                embed = create_success_embed(
                    "購入完了",
                    f"**{plan_name}** を購入しました！\n\n"
                    f"**{hours_text}** 支払額: {price_decimal} {symbol}\n"
                    f"有効期限: {expires_at.strftime('%Y年%m月%d日 %H:%M')}",
                    interaction.user
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                embed = create_error_embed("ロールエラー", "ロールが見つかりませんでした。管理者に連絡してください。", interaction.user)
                await interaction.response.send_message(embed=embed, ephemeral=True)


class RolePlanSelectModal(discord.ui.Modal, title="ロールプラン選択"):
    """ロールプラン選択モーダル"""
    
    plan_id = discord.ui.TextInput(
        label="プランID",
        placeholder="購入したいプランのIDを入力してください",
        required=True,
        max_length=10
    )
    
    def __init__(self, panel_id: int):
        super().__init__()
        self.panel_id = panel_id
    
    async def on_submit(self, interaction: discord.Interaction):
        """モーダル送信時の処理"""
        try:
            plan_id = int(self.plan_id.value)
        except ValueError:
            embed = create_error_embed("入力エラー", "プランIDは数値で入力してください。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # プラン情報を取得して購入処理を実行
        async with aiosqlite.connect(DB_PATH) as db:
            # プラン情報を取得
                plan = await fetch_one(db, """
                    SELECT rp.id, rp.role_id, rp.price, rp.currency_symbol, rp.duration_hours, rp.guild_id
                    FROM role_plans rp
                    WHERE rp.id = ? AND rp.panel_id = ?
                """, (plan_id, self.panel_id))
                
                if not plan:
                    embed = create_error_embed("プランエラー", "指定されたプランが見つかりません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                plan_id, role_id, price, currency_symbol, duration_hours, guild_id = plan
                price_decimal = Decimal(price)
                
                # 通貨情報を取得
                from database import get_asset
                asset = await get_asset(db, currency_symbol, interaction.guild.id)
                if not asset:
                    embed = create_error_embed("通貨エラー", f"通貨 **{currency_symbol}** が見つかりません。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                asset_id, symbol, asset_name, decimals = asset
                
                # ユーザーの残高を確認
                uid = await upsert_user(db, interaction.user.id)
                user_acc = await ensure_user_account(db, interaction.user.id, interaction.guild.id)
                user_balance = await balance_of(db, user_acc, asset_id)
                
                if user_balance < price_decimal:
                    embed = create_error_embed(
                        "残高不足",
                        f"残高が不足しています。\n必要: {price_decimal} {symbol}\n現在: {user_balance} {symbol}",
                        interaction.user
                    )
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                # Treasuryアカウントを取得
                treasury_acc = await account_id_by_name(db, "treasury", interaction.guild.id)
                
                # 支払い処理
                tx_id = await new_transaction(db, kind="role_purchase", created_by_user_id=uid, unique_hash=None, reference=f"Role purchase: {role_id}")
                await post_ledger(db, tx_id, user_acc, asset_id, -price_decimal)
                await post_ledger(db, tx_id, treasury_acc, asset_id, price_decimal)
                
                # 購入記録を保存
                expires_at = datetime.now(TZ) + timedelta(hours=duration_hours)
                await db.execute("""
                    INSERT INTO role_purchases(user_id, plan_id, guild_id, expires_at)
                    VALUES (?, ?, ?, ?)
                """, (uid, plan_id, str(interaction.guild.id), expires_at.isoformat()))
                
                await db.commit()
                
                # ロールを付与
                role = interaction.guild.get_role(int(role_id))
                if role:
                    await interaction.user.add_roles(role)
                    # 期限表示を整形
                    hours_text = f"{duration_hours}時間"
                    if duration_hours >= 24:
                        days = duration_hours // 24
                        remaining_hours = duration_hours % 24
                        hours_text = f"{days}日" + (f"{remaining_hours}時間" if remaining_hours > 0 else "")
                    
                    embed = create_success_embed(
                        "ロール購入完了",
                        f"**{role.name}** を購入しました！\n\n"
                        f"💰 支払額: {price_decimal} {symbol}\n"
                        f"⏰ 有効期限: {expires_at.strftime('%Y年%m月%d日 %H:%M')}\n"
                        f"📅 期間: {hours_text}",
                        interaction.user
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                else:
                    embed = create_error_embed("ロールエラー", "ロールが見つかりませんでした。管理者に連絡してください。", interaction.user)
                    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==================== 自動報酬View ====================

class AutoRewardView(discord.ui.View):
    """
    自動報酬受け取りボタンのView（永続的）
    """
    
    def __init__(self, reward_id: int):
        super().__init__(timeout=None)  # 永続化
        self.reward_id = reward_id
    
    @discord.ui.button(
        label="🎁 報酬を受け取る",
        style=discord.ButtonStyle.success,
        custom_id="autoreward_claim_button"
    )
    async def claim_reward(self, interaction: discord.Interaction, button: discord.ui.Button):
        """報酬受け取りボタンのコールバック"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このボタンはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("BEGIN"):
                # 報酬設定を取得
                reward = await fetch_one(db, """
                    SELECT ar.id, ar.asset_id, ar.reward_amount, ar.max_claims, ar.current_claims, ar.enabled, a.symbol, a.decimals
                    FROM autorewards ar
                    JOIN assets a ON ar.asset_id = a.id
                    WHERE ar.id = ?
                """, (self.reward_id,))
                
                if not reward:
                    embed = create_error_embed("報酬エラー", "この報酬は削除されました。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                reward_id, asset_id, reward_amount, max_claims, current_claims, enabled, symbol, decimals = reward
                
                if not enabled:
                    embed = create_error_embed("報酬無効", "この報酬は現在無効化されています。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                if max_claims != -1 and current_claims >= max_claims:
                    embed = create_error_embed("受取上限", "この報酬の受取上限に達しました。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                # ユーザーが既に受け取っているかチェック
                uid = await upsert_user(db, interaction.user.id)
                already_claimed = await fetch_one(db, """
                    SELECT id FROM autoreward_claims WHERE reward_id = ? AND user_id = ?
                """, (reward_id, uid))
                
                if already_claimed:
                    embed = create_error_embed("受取済み", "この報酬は既に受け取っています。", interaction.user)
                    return await interaction.response.send_message(embed=embed, ephemeral=True)
                
                # 報酬を付与
                reward_decimal = Decimal(reward_amount).quantize(Decimal(10) ** -decimals, rounding=ROUND_DOWN)
                
                treasury_acc = await account_id_by_name(db, "treasury", interaction.guild.id)
                await auto_refill_treasury_if_needed(db, treasury_acc, asset_id, interaction.guild.id, reward_decimal)
                
                user_acc = await ensure_user_account(db, interaction.user.id, interaction.guild.id)
                tx_id = await new_transaction(db, kind="autoreward", created_by_user_id=uid, unique_hash=None, reference=f"Auto reward {reward_id}")
                await post_ledger(db, tx_id, treasury_acc, asset_id, -reward_decimal)
                await post_ledger(db, tx_id, user_acc, asset_id, reward_decimal)
                
                # 受取記録を保存
                await db.execute("""
                    INSERT INTO autoreward_claims(reward_id, user_id) VALUES (?, ?)
                """, (reward_id, uid))
                
                # 受取回数を更新
                await db.execute("""
                    UPDATE autorewards SET current_claims = current_claims + 1 WHERE id = ?
                """, (reward_id,))
                
                await db.commit()
                
                embed = create_success_embed(
                    "報酬獲得！",
                    f"**{reward_decimal} {symbol}** を受け取りました！",
                    interaction.user
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
