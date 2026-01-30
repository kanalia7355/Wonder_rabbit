"""
データベース関連モジュール

SQLiteデータベースの初期化、クエリ実行、データ操作などを提供します。
"""

import aiosqlite
from decimal import Decimal
from config import DB_PATH, DEFAULT_DECIMALS


# ==================== クエリヘルパー ====================

async def fetch_one(db, q, params=None):
    """
    単一行を取得
    
    Args:
        db: データベース接続
        q: SQLクエリ
        params: パラメータ
        
    Returns:
        取得した行（タプル）またはNone
    """
    cur = await db.execute(q, params or {})
    row = await cur.fetchone()
    await cur.close()
    return row


async def fetch_all(db, q, params=None):
    """
    複数行を取得
    
    Args:
        db: データベース接続
        q: SQLクエリ
        params: パラメータ
        
    Returns:
        取得した行のリスト
    """
    cur = await db.execute(q, params or {})
    rows = await cur.fetchall()
    await cur.close()
    return rows


# ==================== ユーザー管理 ====================

async def upsert_user(db, discord_user_id: int) -> int:
    """
    ユーザーをDBに追加（既存の場合は何もしない）
    
    Args:
        db: データベース接続
        discord_user_id: DiscordユーザーID
        
    Returns:
        ユーザーの内部ID
    """
    await db.execute(
        "INSERT OR IGNORE INTO users(discord_user_id) VALUES (?)",
        (str(discord_user_id),),
    )
    row = await fetch_one(db, "SELECT id FROM users WHERE discord_user_id=?", (str(discord_user_id),))
    return int(row[0])


# ==================== 通貨管理 ====================

async def get_asset(db, symbol: str, guild_id: int):
    """
    通貨情報を取得
    
    Args:
        db: データベース接続
        symbol: 通貨シンボル
        guild_id: ギルドID
        
    Returns:
        (id, symbol, name, decimals) のタプル、または None
    """
    return await fetch_one(
        db,
        "SELECT id, symbol, name, decimals FROM assets WHERE symbol=? AND guild_id=?",
        (symbol.upper(), str(guild_id))
    )


async def get_asset_info_by_id(db, asset_id: int):
    """
    asset_idから通貨のsymbolとdecimalsを取得
    
    Args:
        db: データベース接続
        asset_id: 通貨ID
        
    Returns:
        (symbol, decimals) のタプル、または None
    """
    return await fetch_one(db, "SELECT symbol, decimals FROM assets WHERE id = ?", (asset_id,))


async def create_asset(db, symbol: str, name: str, guild_id: int, decimals: int = DEFAULT_DECIMALS):
    """
    新しい通貨を作成
    
    Args:
        db: データベース接続
        symbol: 通貨シンボル
        name: 通貨名
        guild_id: ギルドID
        decimals: 小数点以下の桁数
    """
    await db.execute(
        "INSERT INTO assets(guild_id, symbol, name, decimals) VALUES (?,?,?,?)",
        (str(guild_id), symbol.upper(), name, int(decimals)),
    )


# ==================== アカウント管理 ====================

async def ensure_system_accounts(db, guild_id: int):
    """
    システムアカウント（Treasury、Burn）を作成
    
    Args:
        db: データベース接続
        guild_id: ギルドID
    """
    await db.execute(
        "INSERT OR IGNORE INTO accounts(user_id, guild_id, name, type) VALUES (NULL, ?, ?, 'treasury')",
        (str(guild_id), f"treasury:{guild_id}")
    )
    await db.execute(
        "INSERT OR IGNORE INTO accounts(user_id, guild_id, name, type) VALUES (NULL, ?, ?, 'burn')",
        (str(guild_id), f"burn:{guild_id}")
    )


async def ensure_user_account(db, discord_user_id: int, guild_id: int) -> int:
    """
    ユーザーアカウントを作成（既存の場合は何もしない）
    
    Args:
        db: データベース接続
        discord_user_id: DiscordユーザーID
        guild_id: ギルドID
        
    Returns:
        アカウントID
    """
    uid = await upsert_user(db, discord_user_id)
    name = f"user:{discord_user_id}:{guild_id}"
    await db.execute(
        "INSERT OR IGNORE INTO accounts(user_id, guild_id, name, type) VALUES (?,?,?, 'user')",
        (uid, str(guild_id), name),
    )
    row = await fetch_one(db, "SELECT id FROM accounts WHERE name=?", (name,))
    return int(row[0])


async def account_id_by_name(db, name: str, guild_id: int) -> int:
    """
    アカウント名からIDを取得
    
    Args:
        db: データベース接続
        name: アカウント名（例: "treasury", "burn"）
        guild_id: ギルドID
        
    Returns:
        アカウントID
        
    Raises:
        RuntimeError: アカウントが見つからない場合
    """
    full_name = f"{name}:{guild_id}"
    row = await fetch_one(db, "SELECT id FROM accounts WHERE name=?", (full_name,))
    if not row:
        raise RuntimeError(f"口座が見つかりません: {full_name}")
    return int(row[0])


# ==================== 残高管理 ====================

async def balance_of(db, account_id: int, asset_id: int) -> Decimal:
    """
    アカウントの残高を取得
    
    Args:
        db: データベース接続
        account_id: アカウントID
        asset_id: 通貨ID
        
    Returns:
        残高（Decimal）
    """
    row = await fetch_one(
        db,
        "SELECT COALESCE(SUM(CAST(amount AS TEXT)), '0') FROM ledger_entries WHERE account_id=? AND asset_id=?",
        (account_id, asset_id),
    )
    return Decimal(row[0]) if row and row[0] is not None else Decimal("0")


async def auto_refill_treasury_if_needed(
    db,
    treasury_acc_id: int,
    asset_id: int,
    guild_id: int,
    required_amount: Decimal = None
) -> bool:
    """
    Treasury残高が不足している場合、自動で10億枚発行する
    
    Args:
        db: データベース接続
        treasury_acc_id: TreasuryアカウントID
        asset_id: 通貨ID
        guild_id: ギルドID
        required_amount: 必要な金額（オプション）
        
    Returns:
        補充が行われた場合True、それ以外False
    """
    current_balance = await balance_of(db, treasury_acc_id, asset_id)
    
    # 必要な金額が指定されていて、それが現在の残高を上回る場合、または残高がゼロの場合
    should_refill = False
    if required_amount and current_balance < required_amount:
        should_refill = True
    elif current_balance <= Decimal("0"):
        should_refill = True
    
    if should_refill:
        refill_amount = Decimal("1000000000")  # 10億枚
        
        # 通貨情報を取得
        asset_info = await fetch_one(db, "SELECT symbol FROM assets WHERE id = ?", (asset_id,))
        symbol = asset_info[0] if asset_info else "UNKNOWN"
        
        # システムによる自動発行取引を作成
        tx_id = await new_transaction(
            db,
            kind="auto_treasury_refill",
            created_by_user_id=None,
            unique_hash=None,
            reference=f"Auto refill {refill_amount} {symbol}"
        )
        
        # Treasuryアカウントに10億枚追加
        await post_ledger(db, tx_id, treasury_acc_id, asset_id, refill_amount)
        
        # ここでコミットが必要（refill_amountを確定させるため）
        await db.commit()
        
        print(f"[TREASURY] Auto-refilled {refill_amount} {symbol} to Treasury in guild {guild_id}")
        print(f"[TREASURY] New balance: {current_balance + refill_amount} {symbol}")
        
        return True
    
    return False


# ==================== 取引管理 ====================

async def new_transaction(
    db,
    kind: str,
    created_by_user_id: int | None,
    unique_hash: str | None,
    reference: str | None
) -> int:
    """
    新しい取引を作成
    
    Args:
        db: データベース接続
        kind: 取引種別
        created_by_user_id: 作成者のユーザーID
        unique_hash: ユニークハッシュ
        reference: 参照情報
        
    Returns:
        取引ID
    """
    await db.execute(
        "INSERT INTO transactions(kind, reference, created_by, unique_hash) VALUES (?,?,?,?)",
        (kind, reference, created_by_user_id, unique_hash),
    )
    row = await fetch_one(db, "SELECT last_insert_rowid()")
    return int(row[0])


async def post_ledger(db, tx_id: int, account_id: int, asset_id: int, amount: Decimal):
    """
    仕訳を記帳
    
    Args:
        db: データベース接続
        tx_id: 取引ID
        account_id: アカウントID
        asset_id: 通貨ID
        amount: 金額（Decimal）
    """
    await db.execute(
        "INSERT INTO ledger_entries(tx_id, account_id, asset_id, amount) VALUES (?,?,?,?)",
        (tx_id, account_id, asset_id, str(amount)),
    )


# ==================== ギルド設定 ====================

async def ensure_guild_setup(guild_id: int):
    """
    ギルドの初期設定を確実に行う
    
    Args:
        guild_id: ギルドID
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_system_accounts(db, guild_id)
        await db.commit()


# ==================== データベース初期化 ====================

async def ensure_db():
    """
    データベースを初期化（テーブル作成）
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # usersテーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id TEXT UNIQUE NOT NULL
            )
        """)
        
        # accountsテーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                guild_id TEXT NOT NULL,
                name TEXT UNIQUE NOT NULL,
                type TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        
        # assetsテーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                decimals INTEGER NOT NULL DEFAULT 2,
                UNIQUE(guild_id, symbol)
            )
        """)
        
        # transactionsテーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                reference TEXT,
                created_by INTEGER,
                unique_hash TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (created_by) REFERENCES users(id)
            )
        """)
        
        # ledger_entriesテーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ledger_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                asset_id INTEGER NOT NULL,
                amount TEXT NOT NULL,
                FOREIGN KEY (tx_id) REFERENCES transactions(id),
                FOREIGN KEY (account_id) REFERENCES accounts(id),
                FOREIGN KEY (asset_id) REFERENCES assets(id)
            )
        """)
        
        # claimsテーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                from_user_id INTEGER NOT NULL,
                to_user_id INTEGER NOT NULL,
                asset_id INTEGER NOT NULL,
                amount TEXT NOT NULL,
                memo TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (from_user_id) REFERENCES users(id),
                FOREIGN KEY (to_user_id) REFERENCES users(id),
                FOREIGN KEY (asset_id) REFERENCES assets(id)
            )
        """)
        
        # daily_role_rewardsテーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_role_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                role_id TEXT NOT NULL,
                asset_id INTEGER NOT NULL,
                reward_amount TEXT NOT NULL,
                day_of_week TEXT NOT NULL DEFAULT 'all',
                FOREIGN KEY (asset_id) REFERENCES assets(id),
                UNIQUE(guild_id, role_id, asset_id, day_of_week)
            )
        """)
        
        # daily_logテーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                asset_id INTEGER NOT NULL,
                last_claimed_date TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (asset_id) REFERENCES assets(id),
                UNIQUE(user_id, asset_id)
            )
        """)
        
        # autorewardsテーブル（メッセージIDベース）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS autorewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                asset_id INTEGER NOT NULL,
                reward_amount TEXT NOT NULL,
                max_claims INTEGER DEFAULT -1,
                current_claims INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (asset_id) REFERENCES assets(id),
                UNIQUE(guild_id, message_id)
            )
        """)
        
        # auto_reward_configsテーブル（メッセージトリガーベース）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS auto_reward_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                trigger_message TEXT NOT NULL,
                reward_amount TEXT NOT NULL,
                asset_id INTEGER NOT NULL,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (asset_id) REFERENCES assets(id),
                UNIQUE(guild_id, channel_id)
            )
        """)
        
        # auto_reward_claimsテーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS auto_reward_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                guild_id TEXT NOT NULL,
                claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (config_id) REFERENCES auto_reward_configs(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id),
                UNIQUE(config_id, user_id)
            )
        """)
        
        # role_panelsテーブル（ロール購入パネル）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS role_panels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                panel_id INTEGER NOT NULL,
                panel_name TEXT NOT NULL,
                role_id TEXT NOT NULL,
                currency_symbol TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, panel_id),
                UNIQUE(guild_id, panel_name)
            )
        """)
        
        # role_plansテーブル（ロールプラン）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS role_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                panel_id INTEGER NOT NULL,
                plan_name TEXT NOT NULL,
                role_id TEXT NOT NULL,
                price TEXT NOT NULL,
                currency_symbol TEXT NOT NULL,
                duration_hours INTEGER NOT NULL,
                description TEXT,
                guild_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (panel_id) REFERENCES role_panels(id) ON DELETE CASCADE
            )
        """)
        
        # role_purchasesテーブル（ロール購入履歴）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS role_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_id INTEGER NOT NULL,
                guild_id TEXT NOT NULL,
                purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (plan_id) REFERENCES role_plans(id)
            )
        """)
        
        # temporary_rolesテーブル（一時的なロール管理）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS temporary_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # deployed_panelsテーブル（デプロイ済みパネル管理）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS deployed_panels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                panel_db_id INTEGER NOT NULL,
                deployed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (panel_db_id) REFERENCES role_panels(id) ON DELETE CASCADE
            )
        """)
        
        # vc_sessionsテーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS vc_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                duration_minutes INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        
        # vc_excluded_channelsテーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS vc_excluded_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                channel_type TEXT NOT NULL,
                UNIQUE(guild_id, channel_id)
            )
        """)
        
        # vc_check_rolesテーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS vc_check_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                role_id TEXT NOT NULL,
                UNIQUE(guild_id, role_id)
            )
        """)
        
        # forum_settingsテーブル
        await db.execute("""
            CREATE TABLE IF NOT EXISTS forum_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                forum_channel_id TEXT NOT NULL,
                role_id TEXT NOT NULL,
                delete_old_posts INTEGER DEFAULT 0,
                UNIQUE(guild_id, forum_channel_id, role_id)
            )
        """)
        
        # boost_logsテーブル（ブースト履歴）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS boost_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                boosted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        
        # bank_accountsテーブル（銀行口座）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bank_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                asset_id INTEGER NOT NULL,
                balance TEXT NOT NULL DEFAULT '0',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (asset_id) REFERENCES assets(id),
                UNIQUE(user_id, asset_id)
            )
        """)
        
        # bank_transactionsテーブル（銀行取引履歴）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bank_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                asset_id INTEGER NOT NULL,
                transaction_type TEXT NOT NULL,
                amount TEXT NOT NULL,
                balance_after TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (asset_id) REFERENCES assets(id)
            )
        """)
        
        # vc_earning_sessionsテーブル（VCセッション管理）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS vc_earning_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id TEXT NOT NULL,
                category_id TEXT,
                started_at TIMESTAMP NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                UNIQUE(guild_id, user_id)
            )
        """)
        
        # vc_earning_ratesテーブル（VC報酬レート設定）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS vc_earning_rates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                category_id TEXT NOT NULL,
                asset_id INTEGER NOT NULL,
                rate_per_minute TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (asset_id) REFERENCES assets(id),
                UNIQUE(guild_id, category_id)
            )
        """)
        
        # vc_earning_dailyテーブル（日次獲得記録）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS vc_earning_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                asset_id INTEGER NOT NULL,
                total_earned TEXT NOT NULL DEFAULT '0',
                date TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (asset_id) REFERENCES assets(id),
                UNIQUE(guild_id, user_id, asset_id, date)
            )
        """)
        
        # bank_manager_rolesテーブル（銀行管理ロール）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bank_manager_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                role_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, role_id)
            )
        """)
        
        # sleep_move_preferencesテーブル（ユーザーのスリープVC設定）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sleep_move_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                vc_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, user_id)
            )
        """)
        
        # sleep_move_defaultsテーブル（デフォルトスリープVC）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sleep_move_defaults (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL UNIQUE,
                vc_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # sleep_move_penaltiesテーブル（ペナルティ設定）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sleep_move_penalties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL UNIQUE,
                enabled INTEGER DEFAULT 0,
                penalty_type TEXT NOT NULL,
                base_amount TEXT NOT NULL,
                currency_symbol TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # sleep_move_logsテーブル（移動ログ）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sleep_move_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                move_count INTEGER DEFAULT 0,
                total_penalty TEXT DEFAULT '0',
                last_moved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, user_id)
            )
        """)
        
        await db.commit()
        print("[DATABASE] データベース初期化完了")
