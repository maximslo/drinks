"""Diagnostic: print attributedBody hex for a few recent messages with NULL text."""
import sqlite3, os, sys

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
CHAT_ID = "chat313739884378608609"

conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
rows = conn.execute("""
    SELECT m.ROWID, m.text, m.attributedBody
    FROM message m
    JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
    JOIN chat c ON cmj.chat_id = c.ROWID
    WHERE c.chat_identifier = ?
      AND m.text IS NULL
      AND m.attributedBody IS NOT NULL
      AND (m.associated_message_type = 0 OR m.associated_message_type IS NULL)
    ORDER BY m.ROWID DESC
    LIMIT 5
""", (CHAT_ID,)).fetchall()
conn.close()

for rowid, text, ab in rows:
    print(f"\nROWID={rowid} text={text!r}")
    print(f"  attributedBody length: {len(ab)} bytes")
    print(f"  first 80 hex: {ab[:80].hex()}")
    print(f"  first 80 raw: {ab[:80]!r}")
    # show any readable substrings
    readable = []
    i = 0
    while i < len(ab):
        if 0x20 <= ab[i] <= 0x7e:
            j = i
            while j < len(ab) and (0x20 <= ab[j] <= 0x7e or ab[j] > 0x7f):
                j += 1
            if j - i >= 3:
                readable.append((i, ab[i:j]))
            i = j
        else:
            i += 1
    print(f"  readable strings: {[(off, s) for off, s in readable[:10]]}")
