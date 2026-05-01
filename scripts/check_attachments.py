import sqlite3, os

CHAT_DB = "/Users/admin/Library/Messages/chat.db"
MESSAGES_DB = "/Users/admin/drinks/data/messages.db"

msg_conn = sqlite3.connect(MESSAGES_DB)
null_rowids = [r[0] for r in msg_conn.execute(
    "SELECT rowid FROM messages WHERE has_attachment=1 AND attachment_path IS NULL ORDER BY rowid DESC LIMIT 20"
).fetchall()]
msg_conn.close()

chat_conn = sqlite3.connect(CHAT_DB)
for rowid in null_rowids:
    rows = chat_conn.execute("""
        SELECT a.filename, a.mime_type, a.transfer_state
        FROM message_attachment_join maj
        JOIN attachment a ON maj.attachment_id = a.ROWID
        WHERE maj.message_id = ?
    """, (rowid,)).fetchall()
    if rows:
        for filename, mime, state in rows:
            exists = os.path.exists(os.path.expanduser(filename)) if filename else False
            print(f"ROWID={rowid} mime={mime} state={state} exists={exists} file={filename}")
    else:
        print(f"ROWID={rowid} NO ATTACHMENT RECORD IN chat.db")
chat_conn.close()
