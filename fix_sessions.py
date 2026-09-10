import sqlite3
conn = sqlite3.connect("server/data/params.db")
n = conn.execute(
    "UPDATE sessions SET file_name=? WHERE file_name=? AND status=?",
    ("combined_print.gcode", "unknown", "running")
).rowcount
conn.commit()
print(f"Updated {n} sessions to combined_print.gcode")
conn.close()
