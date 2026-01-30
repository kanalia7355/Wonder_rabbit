"""
VC自動作成Cog

ボタンでプランを選択してVCを自動作成し、通貨を引き落とすシステムを提供します。
role_panel.pyの設計を参考に、パネル名でグループ化。
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
from decimal import Decimal
from datetime import datetime, timedelta
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


class VCCreatorCog(commands.Cog):
    """VC自動作成コマンド群"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cleanup_expired_vcs.start()
    
    def cog_unload(self):
        """Cogアンロード時にタスクを停止"""
        self.cleanup_expired_vcs.cancel()
    
    @tasks.loop(minutes=5)
    async def cleanup_expired_vcs(self):
        """期限切れのVCを削除"""
        now = datetime.now(TZ)
        
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                expired_vcs = await fetch_all(db, """
                    SELECT guild_id, channel_id, id
                    FROM active_vcs
                    WHERE expires_at <= ?
                """, (now.isoformat(),))
                
                for guild_id, channel_id, vc_id in expired_vcs:
                    guild = self.bot.get_guild(int(guild_id))
                    if guild:
                        channel = guild.get_channel(int(channel_id))
                        if channel:
                            try:
                                await channel.delete(reason="有効期限切れ")
                                logger.info(f"[VC_CREATOR] 期限切れVCを削除: {channel.name} (ID: {channel_id})")
                            except Exception as e:
                                logger.error(f"[VC_CREATOR] VC削除エラー: {e}")
                    
                    # DBから削除
                    await db.execute("DELETE FROM active_vcs WHERE id = ?", (vc_id,))
                
                if expired_vcs:
                    await db.commit()
                    logger.info(f"[VC_CREATOR] {len(expired_vcs)}個の期限切れVCを削除しました")
        except Exception as e:
            logger.error(f"[VC_CREATOR] VC自動削除エラー: {e}")
    
    @cleanup_expired_vcs.before_loop
    async def before_cleanup_expired_vcs(self):
        """タスク開始前にBotの準備を待つ"""
        await self.bot.wait_until_ready()
    
    # オートコンプリート
    async def template_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """テンプレート名のオートコンプリート"""
        if not interaction.guild:
            return []
        
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                rows = await fetch_all(db, """
                    SELECT DISTINCT template_name FROM vc_plans
                    WHERE guild_id = ?
                    ORDER BY template_name
                """, (str(interaction.guild.id),))
                
                choices = [
                    app_commands.Choice(name=template_name, value=template_name)
                    for (template_name,) in rows
                    if current.lower() in template_name.lower()
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
    
    async def plan_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """プラン名のオートコンプリート"""
        if not interaction.guild:
            return []
        
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                rows = await fetch_all(db, """
                    SELECT plan_name FROM vc_plans
                    WHERE guild_id = ?
                    ORDER BY plan_name
                """, (str(interaction.guild.id),))
                
                choices = [
                    app_commands.Choice(name=plan_name, value=plan_name)
                    for (plan_name,) in rows
                    if current.lower() in plan_name.lower()
                ]
                
                return choices[:25]
        except:
            return []
    
    # コマンドグループ
    vc_template_group = app_commands.Group(
        name="vc_template",
        description="VCテンプレート管理（管理者のみ）",
        default_permissions=discord.Permissions(administrator=True)
    )
    
    vc_plan_group = app_commands.Group(
        name="vc_plan",
        description="VCプラン管理（管理者のみ）",
        default_permissions=discord.Permissions(administrator=True)
    )
    
    vc_panel_group = app_commands.Group(
        name="vc_panel",
        description="VCパネル管理（管理者のみ）",
        default_permissions=discord.Permissions(administrator=True)
    )
    
    @vc_template_group.command(name="list", description="VCテンプレート一覧を表示（管理者のみ）")
    async def template_list(self, interaction: discord.Interaction):
        """VCテンプレート一覧を表示"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            templates = await fetch_all(db, """
                SELECT template_name, COUNT(*) as plan_count
                FROM vc_plans
                WHERE guild_id = ?
                GROUP BY template_name
                ORDER BY template_name
            """, (str(interaction.guild.id),))
            
            if not templates:
                embed = create_info_embed(
                    "VCテンプレート一覧",
                    "設定されているVCテンプレートはありません。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            embed = discord.Embed(
                title="📋 VCテンプレート一覧",
                color=discord.Color.blue()
            )
            
            for template_name, plan_count in templates:
                embed.add_field(
                    name=f"📁 {template_name}",
                    value=f"プラン数: {plan_count}個",
                    inline=True
                )
            
            embed.set_footer(text=f"実行者: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @vc_plan_group.command(name="create", description="VCプランを作成（管理者のみ）")
    @app_commands.describe(
        template_name="テンプレート名",
        plan_name="プラン名",
        vc_name_template="VC名のテンプレート（{user}でユーザー名）",
        price="料金",
        currency_symbol="通貨シンボル",
        duration_hours="有効期限（時間）",
        permission_type="権限タイプ（basic/secret/freedom）",
        user_limit="ユーザー制限（0で無制限）",
        free_role="無料ロール（オプション）",
        category="カテゴリ（オプション）"
    )
    @app_commands.autocomplete(
        template_name=template_autocomplete,
        currency_symbol=currency_autocomplete
    )
    async def create_plan(
        self,
        interaction: discord.Interaction,
        template_name: str,
        plan_name: str,
        vc_name_template: str,
        price: str,
        currency_symbol: str,
        duration_hours: int,
        permission_type: str,
        user_limit: int = 0,
        free_role: discord.Role = None,
        category: discord.CategoryChannel = None
    ):
        """VCプランを作成"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # 権限タイプの検証
        if permission_type not in ['basic', 'secret', 'freedom']:
            embed = create_error_embed("入力エラー", "権限タイプは basic, secret, freedom のいずれかを指定してください。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # 金額の検証
        try:
            price_decimal = to_decimal(price)
            if price_decimal < 0:
                raise ValueError("金額は0以上である必要があります")
        except Exception as e:
            embed = create_error_embed("入力エラー", f"無効な金額です: {e}", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 通貨が存在するか確認
            asset = await get_asset(db, currency_symbol, interaction.guild.id)
            if not asset:
                embed = create_error_embed("通貨エラー", f"通貨 `{currency_symbol}` が見つかりません。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # プランを追加
            try:
                await db.execute("""
                    INSERT INTO vc_plans(
                        guild_id, template_name, plan_name, vc_name_template, price, currency_symbol,
                        duration_hours, user_limit, free_role_id, category_id, permission_type
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(interaction.guild.id), template_name, plan_name, vc_name_template, str(price_decimal),
                    currency_symbol, duration_hours, user_limit,
                    str(free_role.id) if free_role else None,
                    str(category.id) if category else None,
                    permission_type
                ))
                await db.commit()
            except aiosqlite.IntegrityError:
                embed = create_error_embed("作成エラー", f"プラン `{plan_name}` は既に存在します。", interaction.user)
                return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # 権限タイプの説明
        permission_desc = {
            'basic': '基本権限（管理権限なし）',
            'secret': '非表示 + ユーザー招待可能',
            'freedom': '完全な権限管理が可能'
        }
        
        embed = create_success_embed(
            "VCプラン作成完了",
            f"**テンプレート:** {template_name}\n"
            f"**プラン名:** {plan_name}\n"
            f"**VC名:** {vc_name_template}\n"
            f"**料金:** {price_decimal} {currency_symbol}\n"
            f"**有効期限:** {duration_hours}時間\n"
            f"**権限タイプ:** {permission_type} ({permission_desc[permission_type]})\n"
            f"**ユーザー制限:** {user_limit if user_limit > 0 else '無制限'}\n"
            f"**無料ロール:** {free_role.mention if free_role else 'なし'}\n"
            f"**カテゴリ:** {category.name if category else 'なし'}",
            interaction.user
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @vc_plan_group.command(name="list", description="VCプラン一覧を表示（管理者のみ）")
    @app_commands.describe(template_name="テンプレート名（省略時は全て表示）")
    @app_commands.autocomplete(template_name=template_autocomplete)
    async def list_plans(self, interaction: discord.Interaction, template_name: str = None):
        """VCプラン一覧を表示"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            if template_name:
                plans = await fetch_all(db, """
                    SELECT template_name, plan_name, vc_name_template, price, currency_symbol, duration_hours,
                           user_limit, free_role_id, permission_type
                    FROM vc_plans
                    WHERE guild_id = ? AND template_name = ?
                    ORDER BY permission_type, plan_name
                """, (str(interaction.guild.id), template_name))
            else:
                plans = await fetch_all(db, """
                    SELECT template_name, plan_name, vc_name_template, price, currency_symbol, duration_hours,
                           user_limit, free_role_id, permission_type
                    FROM vc_plans
                    WHERE guild_id = ?
                    ORDER BY template_name, permission_type, plan_name
                """, (str(interaction.guild.id),))
            
            if not plans:
                embed = create_info_embed(
                    "VCプラン一覧",
                    "設定されているVCプランはありません。",
                    interaction.user
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            embed = discord.Embed(
                title=f"📋 VCプラン一覧{f' - {template_name}' if template_name else ''}",
                color=discord.Color.blue()
            )
            
            for template, plan_name, vc_template, price, symbol, duration, limit, free_role_id, perm_type in plans:
                free_role = interaction.guild.get_role(int(free_role_id)) if free_role_id else None
                
                perm_emoji = {
                    'basic': '🔒',
                    'secret': '🔐',
                    'freedom': '🌟'
                }
                
                embed.add_field(
                    name=f"{perm_emoji.get(perm_type, '🔒')} {plan_name}",
                    value=(
                        f"**テンプレート:** {template}\n"
                        f"**料金:** {duration}時間 {price} {symbol}\n"
                        f"**制限:** {limit if limit > 0 else '無制限'}人\n"
                        f"**無料:** {free_role.mention if free_role else 'なし'}\n"
                        f"**権限:** {perm_type}"
                    ),
                    inline=True
                )
            
            embed.set_footer(text=f"実行者: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @vc_plan_group.command(name="delete", description="VCプランを削除（管理者のみ）")
    @app_commands.describe(plan_name="削除するプラン名")
    @app_commands.autocomplete(plan_name=plan_autocomplete)
    async def delete_plan(self, interaction: discord.Interaction, plan_name: str):
        """VCプランを削除"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                DELETE FROM vc_plans
                WHERE guild_id = ? AND plan_name = ?
            """, (str(interaction.guild.id), plan_name))
            
            deleted = cursor.rowcount
            await db.commit()
            
            if deleted > 0:
                embed = create_success_embed(
                    "VCプラン削除完了",
                    f"プラン `{plan_name}` を削除しました。",
                    interaction.user
                )
            else:
                embed = create_error_embed(
                    "削除エラー",
                    f"プラン `{plan_name}` が見つかりませんでした。",
                    interaction.user
                )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @vc_panel_group.command(name="deploy", description="VCパネルを設置（管理者のみ）")
    @app_commands.describe(
        template_name="テンプレート名",
        title="パネルのタイトル",
        description="パネルの説明文"
    )
    @app_commands.autocomplete(template_name=template_autocomplete)
    async def deploy_panel(
        self,
        interaction: discord.Interaction,
        template_name: str,
        title: str,
        description: str
    ):
        """VCパネルを設置"""
        if not interaction.guild:
            embed = create_error_embed("実行エラー", "このコマンドはサーバー内でのみ使用できます。", interaction.user)
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # 指定したテンプレートのプランを取得
            plans = await fetch_all(db, """
                SELECT id, plan_name, price, currency_symbol, duration_hours, permission_type
                FROM vc_plans
                WHERE guild_id = ? AND template_name = ?
                ORDER BY permission_type, duration_hours
            """, (str(interaction.guild.id), template_name))
            
            if not plans:
                embed = create_error_embed(
                    "設置エラー",
                    f"テンプレート `{template_name}` にプランが1つも作成されていません。\n先に `/vc_plan create` でプランを作成してください。",
                    interaction.user
                )
                return await interaction.followup.send(embed=embed, ephemeral=True)
            
            # プラン一覧を作成
            plan_list = []
            for plan_id, plan_name, price, symbol, duration, perm_type in plans:
                plan_list.append(f"{plan_name}: {duration}時間 - {price} {symbol}")
            
            # Embedを作成
            full_description = description + "\n\n" + "\n".join(plan_list)
            
            panel_embed = discord.Embed(
                title=title,
                description=full_description,
                color=discord.Color.blue()
            )
            panel_embed.set_footer(text="ボタンを押してVCを作成")
            
            # ボタンを作成
            view = VCPanelView(plans)
            
            # パネルを送信
            message = await interaction.channel.send(embed=panel_embed, view=view)
            
            # DBに記録
            await db.execute("""
                INSERT INTO vc_panel_deployments(guild_id, channel_id, message_id, title, description)
                VALUES (?, ?, ?, ?, ?)
            """, (str(interaction.guild.id), str(interaction.channel.id), str(message.id), title, description))
            
            await db.commit()
        
        embed = create_success_embed(
            "パネル設置完了",
            f"VCパネルを設置しました。\n\n{message.jump_url}",
            interaction.user
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    
    async def create_vc_from_plan(self, interaction: discord.Interaction, plan_id: int):
        """プランからVCを作成"""
        guild = interaction.guild
        user = interaction.user
        
        async with aiosqlite.connect(DB_PATH) as db:
            # プラン情報を取得
            plan = await fetch_one(db, """
                SELECT id, plan_name, vc_name_template, price, currency_symbol, duration_hours,
                       user_limit, free_role_id, category_id, permission_type
                FROM vc_plans
                WHERE id = ?
            """, (plan_id,))
            
            if not plan:
                embed = create_error_embed("エラー", "プランが見つかりませんでした。", user)
                return await interaction.followup.send(embed=embed, ephemeral=True)
            
            (plan_id, plan_name, vc_template, price, symbol, duration, limit,
             free_role_id, category_id, perm_type) = plan
            
            price_decimal = Decimal(price)
            
            # ユーザーIDを取得（無料・有料問わず必要）
            uid = await upsert_user(db, user.id)
            
            # 無料ロールをチェック
            is_free = False
            if free_role_id:
                free_role = guild.get_role(int(free_role_id))
                if free_role and free_role in user.roles:
                    is_free = True
            
            # 通貨を引き落とし
            if not is_free:
                asset = await get_asset(db, symbol, guild.id)
                if not asset:
                    embed = create_error_embed("通貨エラー", f"通貨 `{symbol}` が見つかりません。", user)
                    return await interaction.followup.send(embed=embed, ephemeral=True)
                
                asset_id = asset[0]
                
                # ユーザーアカウントを取得/作成
                user_account_name = f"user:{user.id}:{guild.id}"
                await db.execute(
                    "INSERT OR IGNORE INTO accounts(user_id, guild_id, name, type) VALUES (?,?,?, 'user')",
                    (uid, str(guild.id), user_account_name),
                )
                user_account_row = await fetch_one(db, "SELECT id FROM accounts WHERE name=?", (user_account_name,))
                user_account_id = int(user_account_row[0])
                
                # 残高をチェック
                user_balance = await balance_of(db, user_account_id, asset_id)
                if user_balance < price_decimal:
                    embed = create_error_embed(
                        "残高不足",
                        f"残高が不足しています。\n\n**必要:** {price_decimal} {symbol}\n**残高:** {user_balance} {symbol}",
                        user
                    )
                    return await interaction.followup.send(embed=embed, ephemeral=True)
                
                # Treasuryアカウントを取得
                treasury_account_id = await account_id_by_name(db, 'treasury', guild.id)
                
                # 送金を実行
                tx_id = await new_transaction(
                    db,
                    kind='vc_creation',
                    created_by_user_id=uid,
                    unique_hash=None,
                    reference=f'VC作成: {plan_name}'
                )
                
                # ユーザーから引き出し（マイナス）
                await post_ledger(db, tx_id, user_account_id, asset_id, -price_decimal)
                
                # Treasuryに送金（プラス）
                await post_ledger(db, tx_id, treasury_account_id, asset_id, price_decimal)
                
                await db.commit()
            
            # VCを作成
            vc_name = vc_template.replace('{user}', user.display_name)
            category = guild.get_channel(int(category_id)) if category_id else None
            
            # 権限設定（secretのみ独自設定）
            if perm_type == 'secret':
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    user: discord.PermissionOverwrite(
                        view_channel=True,
                        connect=True,
                        speak=True,
                        manage_permissions=True,
                        manage_channels=False
                    )
                }
            else:
                # basic と freedom はカテゴリー同期を使用
                overwrites = None
            
            # VCを作成
            try:
                # VCチャンネル作成のパラメータ
                create_params = {
                    'name': vc_name,
                    'category': category,
                    'user_limit': limit if limit > 0 else None
                }
                
                # secretの場合のみoverwritesを設定
                if overwrites is not None:
                    create_params['overwrites'] = overwrites
                
                vc = await guild.create_voice_channel(**create_params)
                
                # カテゴリーの権限に同期（basic と freedom）
                if category and perm_type in ['basic', 'freedom']:
                    await vc.edit(sync_permissions=True)
                
                # ユーザー固有の権限を設定
                if perm_type == 'basic':
                    await vc.set_permissions(user, view_channel=True, connect=True, speak=True, manage_channels=False)
                elif perm_type == 'freedom':
                    await vc.set_permissions(user, view_channel=True, connect=True, speak=True, manage_channels=True, manage_permissions=True)
            except Exception as e:
                embed = create_error_embed("作成エラー", f"VCの作成に失敗しました:\n```\n{str(e)}\n```", user)
                return await interaction.followup.send(embed=embed, ephemeral=True)
            
            # 有効期限を計算
            expires_at = datetime.now(TZ) + timedelta(hours=duration)
            
            # DBに記録
            await db.execute("""
                INSERT INTO active_vcs(guild_id, channel_id, owner_user_id, plan_id, expires_at)
                VALUES (?, ?, ?, ?, ?)
            """, (str(guild.id), str(vc.id), uid, plan_id, expires_at.isoformat()))
            
            await db.commit()
        
        # 完了メッセージ
        embed = create_success_embed(
            "VC作成完了",
            f"**VC:** {vc.mention}\n"
            f"**プラン:** {plan_name}\n"
            f"**料金:** {'無料' if is_free else f'{price_decimal} {symbol}'}\n"
            f"**有効期限:** {duration}時間後",
            user
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class VCPanelView(discord.ui.View):
    """VCパネルのView"""
    
    def __init__(self, plans: list = None):
        super().__init__(timeout=None)
        
        if plans:
            # プラン毎にボタンを追加
            for plan_id, plan_name, price, symbol, duration, perm_type in plans:
                # ボタンラベル: プラン名のみ
                button = discord.ui.Button(
                    label=plan_name,
                    style=discord.ButtonStyle.primary,
                    custom_id=f"vc_create:{plan_id}"
                )
                button.callback = self.create_button_callback(plan_id)
                self.add_item(button)
    
    def create_button_callback(self, plan_id: int):
        async def button_callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            
            cog = interaction.client.get_cog('VCCreatorCog')
            if cog:
                await cog.create_vc_from_plan(interaction, plan_id)
        
        return button_callback


async def setup(bot: commands.Bot):
    """Cogをセットアップ"""
    await bot.add_cog(VCCreatorCog(bot))
