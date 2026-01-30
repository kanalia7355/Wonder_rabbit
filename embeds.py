"""
Embed作成ヘルパーモジュール

Discord Embedの作成を簡単にするヘルパー関数を提供します。
"""

import discord
from datetime import datetime
from config import TZ


def create_success_embed(title: str, description: str, user: discord.User = None) -> discord.Embed:
    """
    成功メッセージ用のEmbedを作成
    
    Args:
        title: Embedのタイトル
        description: Embedの説明
        user: フッターに表示するユーザー（オプション）
        
    Returns:
        作成されたEmbed
    """
    embed = discord.Embed(title=f"✅ {title}", description=description, color=0x2ecc71)
    if user:
        embed.set_footer(text=f"実行者: {user.display_name}", icon_url=user.display_avatar.url)
    embed.timestamp = datetime.now(TZ)
    return embed


def create_error_embed(title: str, description: str, user: discord.User = None) -> discord.Embed:
    """
    エラーメッセージ用のEmbedを作成
    
    Args:
        title: Embedのタイトル
        description: Embedの説明
        user: フッターに表示するユーザー（オプション）
        
    Returns:
        作成されたEmbed
    """
    embed = discord.Embed(title=f"❌ {title}", description=description, color=0xe74c3c)
    if user:
        embed.set_footer(text=f"実行者: {user.display_name}", icon_url=user.display_avatar.url)
    embed.timestamp = datetime.now(TZ)
    return embed


def create_info_embed(title: str, description: str, user: discord.User = None) -> discord.Embed:
    """
    情報表示用のEmbedを作成
    
    Args:
        title: Embedのタイトル
        description: Embedの説明
        user: フッターに表示するユーザー（オプション）
        
    Returns:
        作成されたEmbed
    """
    embed = discord.Embed(title=f"ℹ️ {title}", description=description, color=0x3498db)
    if user:
        embed.set_footer(text=f"実行者: {user.display_name}", icon_url=user.display_avatar.url)
    embed.timestamp = datetime.now(TZ)
    return embed


def create_warning_embed(title: str, description: str, user: discord.User = None) -> discord.Embed:
    """
    警告メッセージ用のEmbedを作成
    
    Args:
        title: Embedのタイトル
        description: Embedの説明
        user: フッターに表示するユーザー（オプション）
        
    Returns:
        作成されたEmbed
    """
    embed = discord.Embed(title=f"⚠️ {title}", description=description, color=0xf39c12)
    if user:
        embed.set_footer(text=f"実行者: {user.display_name}", icon_url=user.display_avatar.url)
    embed.timestamp = datetime.now(TZ)
    return embed


def create_transaction_embed(
    transaction_type: str,
    from_user: str,
    to_user: str,
    amount: str,
    symbol: str,
    memo: str = None,
    executor: discord.User = None
) -> discord.Embed:
    """
    取引用のEmbedを作成
    
    Args:
        transaction_type: 取引タイプ（例: "送金", "発行"）
        from_user: 送信元ユーザー（メンション形式）
        to_user: 送信先ユーザー（メンション形式）
        amount: 金額
        symbol: 通貨シンボル
        memo: メモ（オプション）
        executor: 実行者（オプション）
        
    Returns:
        作成されたEmbed
    """
    embed = discord.Embed(
        title=f"💸 {transaction_type}完了",
        color=0xf39c12
    )
    
    embed.add_field(name="送信元", value=from_user, inline=True)
    embed.add_field(name="送信先", value=to_user, inline=True)
    embed.add_field(name="金額", value=f"**{amount} {symbol}**", inline=True)
    
    if memo:
        embed.add_field(name="📝 メモ", value=memo, inline=False)
    
    if executor:
        embed.set_footer(text=f"実行者: {executor.display_name}", icon_url=executor.display_avatar.url)
    
    embed.timestamp = datetime.now(TZ)
    return embed


def create_shop_embed(
    shop_name: str,
    shop_description: str,
    items: list,
    shop_id: int,
    is_official: bool = False
) -> discord.Embed:
    """
    ショップ表示用のEmbedを作成
    
    Args:
        shop_name: ショップ名
        shop_description: ショップ説明
        items: 商品リスト
        shop_id: ショップID
        is_official: 公式ショップかどうか
        
    Returns:
        作成されたEmbed
    """
    if is_official:
        embed = discord.Embed(
            title=f"⭐ {shop_name}（公式ショップ）",
            description=shop_description or "公式が運営する特別なショップです",
            color=0xffd700  # ゴールド色
        )
    else:
        embed = discord.Embed(
            title=f"🛍️ {shop_name}",
            description=shop_description or "ユーザーが運営するショップです",
            color=0x3498db
        )
    
    if not items:
        embed.add_field(name="📦 商品", value="現在商品がありません", inline=False)
        return embed
    
    for item in items:
        item_id, item_name, price, item_desc, stock, item_status, symbol = item[:7]
        
        # 在庫状況の表示
        if stock == -1:
            stock_text = "♾️ 無制限"
        elif stock > 0:
            stock_text = f"📦 在庫: {stock}"
        else:
            stock_text = "🚫 売り切れ"
        
        status_emoji = "✅" if item_status == 'available' else "❌"
        
        # 説明がある場合のみ表示
        description_line = f"{item_desc}\n" if item_desc else ""
        field_value = f"{description_line}💰 **{price} {symbol}**\n{stock_text}"
        embed.add_field(
            name=f"{status_emoji} {item_name} (ID: {item_id})",
            value=field_value,
            inline=False
        )
    
    embed.set_footer(text=f"購入: /shop buy item_id:(商品ID) | 店舗ID: {shop_id}")
    return embed
