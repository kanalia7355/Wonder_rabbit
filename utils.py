"""
ユーティリティ関数モジュール

汎用的な計算や変換などのヘルパー関数を提供します。
"""

import math
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from config import SALARY_MIN_MINUTES, SALARY_UNIT_MINUTES, SALARY_HOUR_TO_MINUTES


def to_decimal(s: str | float | int) -> Decimal:
    """
    文字列、浮動小数点数、整数をDecimalに変換します。
    
    Args:
        s: 変換する値
        
    Returns:
        Decimal: 変換されたDecimal値
        
    Raises:
        ValueError: 変換できない値の場合
    """
    try:
        d = Decimal(str(s))
        if not d.is_finite():
            raise InvalidOperation
        return d
    except Exception:
        raise ValueError("金額が不正です。")


def calculate_hours_15min_ceil(minutes: int) -> float:
    """
    15分単位での切り上げ時間計算
    
    Args:
        minutes: 作業分数
    
    Returns:
        15分単位で切り上げた時間数
    
    Examples:
        >>> calculate_hours_15min_ceil(14)
        0.0
        >>> calculate_hours_15min_ceil(15)
        0.25
        >>> calculate_hours_15min_ceil(29)
        0.25
        >>> calculate_hours_15min_ceil(30)
        0.5
        >>> calculate_hours_15min_ceil(59)
        1.0
    """
    if minutes < SALARY_MIN_MINUTES:
        return 0.0
    return math.ceil(minutes / SALARY_UNIT_MINUTES) * SALARY_UNIT_MINUTES / SALARY_HOUR_TO_MINUTES


def format_amount(amount: Decimal, decimals: int) -> str:
    """
    金額を指定された小数点桁数でフォーマットします。
    
    Args:
        amount: フォーマットする金額
        decimals: 小数点以下の桁数
        
    Returns:
        フォーマットされた金額文字列
    """
    return str(amount.quantize(Decimal(10) ** -decimals))


def get_duration_display(days: int) -> str:
    """
    日数から期間表示名を生成
    
    Args:
        days: 日数
        
    Returns:
        期間表示文字列（例: "1ヶ月", "3ヶ月", "1年"）
    """
    if days >= 365:
        years = days // 365
        return f"{years}年"
    elif days >= 30:
        months = days // 30
        return f"{months}ヶ月"
    else:
        return f"{days}日"


def format_number_with_commas(number: int | float | Decimal) -> str:
    """
    数値をカンマ区切りでフォーマット
    
    Args:
        number: フォーマットする数値
        
    Returns:
        カンマ区切りの文字列
    """
    return f"{number:,}"


async def has_bank_permission(interaction) -> bool:
    """
    銀行管理権限をチェック
    
    Args:
        interaction: Discord Interaction
        
    Returns:
        管理者権限または銀行管理ロールを持っている場合True
    """
    import discord
    import aiosqlite
    from config import DB_PATH
    from database import fetch_all
    
    # 管理者権限を持っている場合はTrue
    if interaction.user.guild_permissions.administrator:
        return True
    
    # 銀行管理ロールをチェック
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await fetch_all(db, """
            SELECT role_id FROM bank_manager_roles
            WHERE guild_id = ?
        """, (str(interaction.guild_id),))
        
        manager_role_ids = {int(row[0]) for row in rows}
        user_role_ids = {role.id for role in interaction.user.roles}
        
        # ユーザーが銀行管理ロールを持っている場合はTrue
        return bool(manager_role_ids & user_role_ids)

