"""
バックアップ管理モジュール

データベースの定期バックアップと復元機能を提供します。
"""

import os
import shutil
import asyncio
from datetime import datetime, timedelta
from config import DB_PATH, BACKUP_DIR, BACKUP_INTERVAL, BACKUP_RETENTION_DAYS, TZ


def ensure_backup_dir():
    """バックアップディレクトリを作成"""
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)
        print(f"[BACKUP] バックアップディレクトリを作成しました: {BACKUP_DIR}")


def create_backup():
    """
    データベースのバックアップを作成
    
    Returns:
        バックアップファイルのパス
    """
    ensure_backup_dir()
    
    # バックアップファイル名（タイムスタンプ付き）
    timestamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    backup_filename = f"backup_{timestamp}.sqlite3"
    backup_path = os.path.join(BACKUP_DIR, backup_filename)
    
    # データベースファイルをコピー
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, backup_path)
        print(f"[BACKUP] バックアップを作成しました: {backup_filename}")
        return backup_path
    else:
        print(f"[BACKUP] データベースファイルが見つかりません: {DB_PATH}")
        return None


def cleanup_old_backups():
    """古いバックアップファイルを削除"""
    ensure_backup_dir()
    
    cutoff_date = datetime.now(TZ) - timedelta(days=BACKUP_RETENTION_DAYS)
    deleted_count = 0
    
    for filename in os.listdir(BACKUP_DIR):
        if filename.startswith("backup_") and filename.endswith(".sqlite3"):
            filepath = os.path.join(BACKUP_DIR, filename)
            file_mtime = datetime.fromtimestamp(os.path.getmtime(filepath), tz=TZ)
            
            if file_mtime < cutoff_date:
                os.remove(filepath)
                deleted_count += 1
                print(f"[BACKUP] 古いバックアップを削除しました: {filename}")
    
    if deleted_count > 0:
        print(f"[BACKUP] {deleted_count}個のバックアップを削除しました")


def restore_backup(backup_filename: str) -> bool:
    """
    バックアップからデータベースを復元
    
    Args:
        backup_filename: 復元するバックアップファイル名
        
    Returns:
        復元が成功した場合True
    """
    backup_path = os.path.join(BACKUP_DIR, backup_filename)
    
    if not os.path.exists(backup_path):
        print(f"[BACKUP] バックアップファイルが見つかりません: {backup_filename}")
        return False
    
    # 現在のデータベースをバックアップ
    if os.path.exists(DB_PATH):
        temp_backup = f"{DB_PATH}.pre_restore_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(DB_PATH, temp_backup)
        print(f"[BACKUP] 現在のDBをバックアップしました: {temp_backup}")
    
    # バックアップから復元
    shutil.copy2(backup_path, DB_PATH)
    print(f"[BACKUP] データベースを復元しました: {backup_filename}")
    return True


def list_backups() -> list:
    """
    利用可能なバックアップファイルのリストを取得
    
    Returns:
        バックアップファイル名のリスト（新しい順）
    """
    ensure_backup_dir()
    
    backups = []
    for filename in os.listdir(BACKUP_DIR):
        if filename.startswith("backup_") and filename.endswith(".sqlite3"):
            filepath = os.path.join(BACKUP_DIR, filename)
            mtime = os.path.getmtime(filepath)
            backups.append((filename, mtime))
    
    # 新しい順にソート
    backups.sort(key=lambda x: x[1], reverse=True)
    return [b[0] for b in backups]


async def backup_loop():
    """
    定期的にバックアップを作成するループ
    """
    print(f"[BACKUP] バックアップループを開始します（間隔: {BACKUP_INTERVAL}秒）")
    
    while True:
        try:
            await asyncio.sleep(BACKUP_INTERVAL)
            create_backup()
            cleanup_old_backups()
        except Exception as e:
            print(f"[BACKUP] バックアップ中にエラーが発生しました: {e}")


def get_backup_info(backup_filename: str) -> dict:
    """
    バックアップファイルの情報を取得
    
    Args:
        backup_filename: バックアップファイル名
        
    Returns:
        バックアップ情報の辞書
    """
    backup_path = os.path.join(BACKUP_DIR, backup_filename)
    
    if not os.path.exists(backup_path):
        return None
    
    stat = os.stat(backup_path)
    return {
        "filename": backup_filename,
        "size_bytes": stat.st_size,
        "size_mb": round(stat.st_size / (1024 * 1024), 2),
        "created_at": datetime.fromtimestamp(stat.st_mtime, tz=TZ),
        "path": backup_path
    }
