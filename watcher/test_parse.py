import sqlite3
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from parser import parse_drink_text, resolve_name
from watcher import fetch_new_messages, process_messages

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
CHAT_ID = "chat313739884378608609"

chat_conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
messages = fetch_new_messages(chat_conn, 0)
chat_conn.close()

drinks = process_messages(messages)

print(f"\nFound {len(drinks)} drinks total\n")
for d in drinks[:20]:
    print(f"  #{d['drink_number']} {d['person']} - {d['details'] or ''} ({d['date']})")

print(f"\n...and {max(0, len(drinks)-20)} more")

# show flagged log if it exists
flag_log = os.path.expanduser("~/drinks/data/flagged.log")
if os.path.exists(flag_log):
    print("\n--- FLAGGED ---")
    with open(flag_log) as f:
        print(f.read())
