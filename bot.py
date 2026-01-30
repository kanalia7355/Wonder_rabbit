"""
Discord Bot

日本語対応の多機能Discord経済Bot
"""

import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv

from config import BOT_NAME, BOT_VERSION, BOT_DESCRIPTION, DB_PATH
from database import ensure_db
from backup import backup_loop, create_backup
from models import CurrencyDeleteConfirmView, RolePurchaseView, AutoRewardView
from cogs.vc_creator import VCPanelView

# スクリプトのディレクトリを取得
SCRIPT_DIR = Path(__file__).parent.absolute()

# 環境変数を読み込み（スクリプトと同じディレクトリの.envファイル）
env_path = SCRIPT_DIR / '.env'
load_dotenv(dotenv_path=env_path)

# Botのインテント設定
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.guilds = True
intents.emojis_and_stickers = True

# Botインスタンスを作成
bot = commands.Bot(
    command_prefix='!',
    intents=intents,
    description=BOT_DESCRIPTION
)


@bot.event
async def on_ready():
    """Bot起動時の処理"""
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  {BOT_NAME} v{BOT_VERSION}")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  ログイン: {bot.user.name} (ID: {bot.user.id})")
    print(f"  サーバー数: {len(bot.guilds)}")
    print(f"  Discord.py: {discord.__version__}")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    
    # データベース初期化
    print("[INIT] データベースを初期化しています...")
    await ensure_db()
    
    # 永続的なViewを復元（Bot再起動後もボタンが機能するように）
    print("[INIT] 永続的なViewを復元しています...")
    
    # AutoRewardViewを復元
    import aiosqlite
    from database import fetch_all
    async with aiosqlite.connect(DB_PATH) as db:
        # 設置済みの自動報酬を取得
        autorewards = await fetch_all(db, "SELECT DISTINCT id FROM autorewards WHERE enabled = 1")
        for (reward_id,) in autorewards:
            bot.add_view(AutoRewardView(reward_id=reward_id))
        print(f"  ✓ AutoRewardView: {len(autorewards)}件復元")
        
        # 設置済みのロールパネルを取得
        panels = await fetch_all(db, """
            SELECT DISTINCT rp.panel_id 
            FROM deployed_panels dp
            JOIN role_panels rp ON dp.panel_db_id = rp.id
        """)
        for (panel_id,) in panels:
            bot.add_view(RolePurchaseView(panel_id=panel_id))
        print(f"  ✓ RolePurchaseView: {len(panels)}件復元")
        
        # 設置済みのVCパネルを取得
        vc_panel_data = await fetch_all(db, """
            SELECT vpd.message_id, vpd.guild_id
            FROM vc_panel_deployments vpd
        """)
        
        for (message_id, panel_guild_id) in vc_panel_data:
            # パネルに紐づくプランを取得
            plans = await fetch_all(db, """
                SELECT vp.id, vp.plan_name, vp.price, vp.currency_symbol, vp.duration_hours, vp.permission_type
                FROM vc_plans vp
                WHERE vp.guild_id = ?
                ORDER BY vp.id
            """, (panel_guild_id,))
            
            if plans:
                bot.add_view(VCPanelView(plans))
        
        print(f"  ✓ VCPanelView: {len(vc_panel_data)}件復元")
        
    
    # Cogsをロード
    print("[INIT] Cogsをロードしています...")
    cogs_to_load = [
        'cogs.currency',
        'cogs.balance',
        'cogs.bank',
        'cogs.autoreward',
        'cogs.role_panel',
        'cogs.role_expiry',
        'cogs.vc_management',
        'cogs.forum_manager',
        'cogs.emoji_saver',
        'cogs.server_template',
        'cogs.monthly_allowance',
        'cogs.channel_management',
        'cogs.vc_creator',
        'cogs.return_logger',
        'cogs.boost_reward',
        'cogs.dm_sender',
        'cogs.sleep_move',
        'cogs.transaction_logger',
        'cogs.vc_earning'
    ]
    
    for cog in cogs_to_load:
        try:
            await bot.load_extension(cog)
            print(f"  ✓ {cog} をロードしました")
        except Exception as e:
            print(f"  ✗ {cog} のロードに失敗しました: {e}")
    
    # コマンドを同期
    print("[INIT] コマンドを同期しています...")
    try:
        synced = await bot.tree.sync()
        print(f"  ✓ {len(synced)}個のコマンドを同期しました")
    except Exception as e:
        print(f"  ✗ コマンドの同期に失敗しました: {e}")
    
    # 初回バックアップを作成
    print("[BACKUP] 初回バックアップを作成しています...")
    create_backup()
    
    # バックアップループを開始
    print("[BACKUP] バックアップループを開始しています...")
    bot.loop.create_task(backup_loop())
    
    print(f"\n[READY] {BOT_NAME} の起動が完了しました！")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """サーバーに参加した時の処理"""
    print(f"[GUILD] 新しいサーバーに参加しました: {guild.name} (ID: {guild.id})")


@bot.event
async def on_guild_remove(guild: discord.Guild):
    """サーバーから退出した時の処理"""
    print(f"[GUILD] サーバーから退出しました: {guild.name} (ID: {guild.id})")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """VC参加/退出時の処理（VC時間追跡用）"""
    # この機能は後で実装します
    pass


@bot.event
async def on_error(event: str, *args, **kwargs):
    """エラーハンドリング"""
    import traceback
    print(f"[ERROR] イベント '{event}' でエラーが発生しました:")
    traceback.print_exc()


@bot.tree.command(name="help", description="ヘルプを表示（管理者のみ）")
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def help_command(interaction: discord.Interaction):
    """Help command"""
    embed = discord.Embed(
        title=f"📚 {BOT_NAME} ヘルプ",
        description=f"{BOT_DESCRIPTION}\n\nバージョン: {BOT_VERSION}",
        color=0x3498db
    )
    
    # Currency commands
    embed.add_field(
        name="💰 通貨系コマンド",
        value=(
            "`/create` - 新しい通貨を作成（管理者のみ）\n"
            "`/delete` - 通貨を削除（管理者のみ）\n"
            "`/treasury` - Treasury残高を確認（管理者のみ）\n"
            "`/give` - Treasuryから通貨を発行（管理者のみ）\n"
            "`/balance` - 残高を確認\n"
            "`/pay` - 他のユーザーに送金"
        ),
        inline=False
    )
    
    # Auto-reward commands
    embed.add_field(
        name="🎁 自動報酬系コマンド",
        value=(
            "`/autoreward setup` - 自動報酬を設定\n"
            "`/autoreward list` - 報酬一覧を表示\n"
            "`/autoreward enable` - 報酬を有効化\n"
            "`/autoreward disable` - 報酬を無効化\n"
            "`/autoreward remove` - 報酬を削除\n"
            "`/autoreward stats` - 統計を表示\n"
            "`/autoreward edit` - 報酬を編集"
        ),
        inline=False
    )
    
    # Role panel commands
    embed.add_field(
        name="🛒 ロール購入系コマンド",
        value=(
            "`/panel_create` - ロール購入パネルを作成\n"
            "`/plan_add` - プランを追加\n"
            "`/plan_list` - プラン一覧を表示\n"
            "`/panel_list` - パネル一覧を表示\n"
            "`/panel_delete` - パネルを削除\n"
            "`/panel_deploy` - パネルを設置"
        ),
        inline=False
    )
    
    # VC management commands
    embed.add_field(
        name="🎤 VC管理系コマンド",
        value=(
            "`/check` - VC時間を確認\n"
            "`/exclude_add` - 除外チャンネルを追加\n"
            "`/exclude_list` - 除外チャンネル一覧を表示\n"
            "`/exclude_remove` - 除外設定を削除"
        ),
        inline=False
    )
    
    # Forum management commands
    embed.add_field(
        name="📋 フォーラム管理系コマンド",
        value=(
            "`/forum setup` - フォーラムとロールを紐付け\n"
            "`/forum list` - 設定一覧を表示\n"
            "`/forum remove` - 設定を削除"
        ),
        inline=False
    )
    
    # Monthly allowance commands
    embed.add_field(
        name="📅 月次自動送金コマンド",
        value=(
            "`/monthly_allowance setup` - 月次自動送金を設定\n"
            "`/monthly_allowance list` - 設定一覧を表示\n"
            "`/monthly_allowance remove` - 設定を削除\n"
            "`/monthly_allowance history` - 送金履歴を表示\n"
            "`/monthly_allowance execute` - 手動実行"
        ),
        inline=False
    )
    
    # VC creator commands
    embed.add_field(
        name="🎙️ VC自動作成コマンド",
        value=(
            "`/vc_plan create` - VCプランを作成\n"
            "`/vc_plan list` - プラン一覧を表示\n"
            "`/vc_plan delete` - プランを削除\n"
            "`/vc_panel deploy` - パネルを設置"
        ),
        inline=False
    )
    
    embed.set_footer(text=f"実行者: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="情報", description="Botの情報を表示（管理者のみ）")
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def info_command(interaction: discord.Interaction):
    """情報コマンド"""
    embed = discord.Embed(
        title=f"ℹ️ {BOT_NAME} 情報",
        description=BOT_DESCRIPTION,
        color=0x3498db
    )
    
    embed.add_field(name="バージョン", value=BOT_VERSION, inline=True)
    embed.add_field(name="サーバー数", value=f"{len(bot.guilds)}サーバー", inline=True)
    embed.add_field(name="ユーザー数", value=f"{len(bot.users)}ユーザー", inline=True)
    embed.add_field(name="Discord.py", value=discord.__version__, inline=True)
    embed.add_field(name="応答速度", value=f"{round(bot.latency * 1000)}ms", inline=True)
    
    # データベース情報
    if os.path.exists(DB_PATH):
        db_size = os.path.getsize(DB_PATH) / (1024 * 1024)  # MB
        embed.add_field(name="データベース", value=f"{db_size:.2f} MB", inline=True)
    
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text=f"実行者: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)



def main():
    """メイン関数"""
    token = os.getenv('DISCORD_TOKEN')
    
    # デバッグ情報
    print(f"[DEBUG] .envファイルのパス: {env_path}")
    print(f"[DEBUG] .envファイルが存在: {env_path.exists()}")
    print(f"[DEBUG] トークンの長さ: {len(token) if token else 0}")
    
    if not token:
        print("[ERROR] DISCORD_TOKENが設定されていません。")
        print(f"[ERROR] {env_path} にDISCORD_TOKEN=your_token_hereを設定してください。")
        return
    
    if token == "your_bot_token_here":
        print("[ERROR] DISCORD_TOKENがデフォルト値のままです。")
        print(f"[ERROR] {env_path} を編集して、実際のトークンを設定してください。")
        return
    
    try:
        bot.run(token)
    except discord.LoginFailure:
        print("[ERROR] ログインに失敗しました。トークンを確認してください。")
    except KeyboardInterrupt:
        print("\n[INFO] Botを終了しています...")
    except Exception as e:
        print(f"[ERROR] 予期しないエラーが発生しました: {e}")


if __name__ == "__main__":
    main()
