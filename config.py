"""
設定・定数モジュール

Bot全体で使用される設定値と定数を定義します。
"""

import os
from datetime import timezone, timedelta

# タイムゾーン設定
TZ = timezone(timedelta(hours=9))  # 日本標準時(JST)

# データベース設定
DB_PATH = os.getenv("VC_DB", os.path.join(os.path.dirname(__file__), "template.sqlite3"))

# バックアップ設定
BACKUP_DIR = os.path.join(os.path.dirname(__file__), "backups")
BACKUP_INTERVAL = int(os.getenv("BACKUP_INTERVAL", "43200"))  # デフォルト: 12時間
BACKUP_RETENTION_DAYS = 7  # バックアップ保持日数

# 通貨設定
DEFAULT_DECIMALS = 2

# 給料計算設定
SALARY_MIN_MINUTES = 15  # 最低労働時間（分）
SALARY_UNIT_MINUTES = 15  # 計算単位（分）
SALARY_HOUR_TO_MINUTES = 60  # 1時間の分数

# リアルタイムVC追跡用のアクティブセッション管理
# Format: {(guild_id, user_id): {'start_time': datetime, 'channel_id': str, 'session_id': int}}
active_vc_sessions = {}

# 開発者ID（必要に応じて変更）
DEVELOPER_ID = 608195085716422656

# Bot設定
BOT_NAME = "DiscordBot"
BOT_VERSION = "1.0.0"
BOT_DESCRIPTION = "日本語対応の多機能Discord経済Bot"
