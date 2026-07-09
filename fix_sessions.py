import sqlite3
conn = sqlite3.connect(r'D:\UFAMeasy_Machine_Server\server\data\params.db')
conn.execute("UPDATE sessions SET status='ended', ended_at=datetime('now') WHERE status='running'")
conn.commit()
print(conn.execute('SELECT status, COUNT(*) FROM sessions GROUP BY status').fetchall())