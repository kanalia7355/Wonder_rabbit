import sqlite3

conn = sqlite3.connect('economy.db')
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print('Tables:', [t[0] for t in tables])

# vc_plansテーブルが存在するか確認
if 'vc_plans' in [t[0] for t in tables]:
    cursor.execute("PRAGMA table_info(vc_plans)")
    columns = cursor.fetchall()
    print('\nvc_plans columns:')
    for col in columns:
        print(f"  - {col[1]} ({col[2]})")
else:
    print('\nvc_plans table does not exist')

conn.close()
