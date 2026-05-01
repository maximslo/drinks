import sqlite3
import time
import os
import sys
import re
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '../.env'))

sys.path.insert(0, os.path.dirname(__file__))
from parser import parse_drink_text, resolve_name
from ai_parser import parse_ambiguous

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
DRINKS_DB = os.path.expanduser("~/drinks/data/drinks.db")
CHAT_ID = "chat313739884378608609"
POLL_INTERVAL = 30
FLAG_LOG = os.path.expanduser("~/drinks/data/flagged.log")
CONTEXT_WINDOW = 10

def init_drinks_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drinks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            drink_number INTEGER UNIQUE,
            person TEXT,
            details TEXT,
            date TEXT,
            imessage_id INTEGER UNIQUE,
            source TEXT DEFAULT 'auto'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()

def get_last_processed(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='last_imessage_id'").fetchone()
    return int(row[0]) if row else 0

def set_last_processed(conn, imessage_id):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_imessage_id', ?)", (imessage_id,))
    conn.commit()

def fetch_new_messages(chat_conn, last_id):
    rows = chat_conn.execute("""
        SELECT
            message.ROWID,
            handle.id as handle_id,
            message.text,
            message.date,
            message.cache_has_attachments
        FROM message
        JOIN chat_message_join ON message.ROWID = chat_message_join.message_id
        JOIN chat ON chat_message_join.chat_id = chat.ROWID
        LEFT JOIN handle ON message.handle_id = handle.ROWID
        WHERE chat.chat_identifier = ?
        AND message.ROWID > ?
        ORDER BY message.ROWID ASC
    """, (CHAT_ID, last_id)).fetchall()
    return rows

def is_range(text):
    return bool(re.match(r'^\d+\s*-\s*\d+\s*$', text.strip())) if text else False

def is_reaction(text):
    if not text:
        return False
    reactions = ["loved", "liked", "disliked", "laughed at", "emphasized", "questioned", "reacted"]
    return any(text.lower().startswith(r) for r in reactions)

def is_math_request(text):
    """Detect 'someone do the math' style messages."""
    if not text:
        return False
    keywords = ["do the math", "someone count", "figure it out", "math pls", "math please"]
    return any(k in text.lower() for k in keywords)

def apple_ts_to_str(date):
    apple_epoch = 978307200
    ts = apple_epoch + date / 1e9
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def flag(reason, messages):
    with open(FLAG_LOG, "a") as f:
        f.write(f"\n[FLAGGED: {reason}]\n")
        for m in messages:
            f.write(f"  ROWID={m[0]} handle={m[1]} text={m[2]!r}\n")

def resolve_drink_numbers(parsed):
    if not parsed:
        return [], False
    if len(parsed) == 1:
        return parsed, False
    starred = [p for p in parsed if p[4]]
    if starred:
        return [starred[0]], False
    nums = [p[0] for p in parsed]
    if all(nums[k] - nums[k+1] == 1 for k in range(len(nums)-1)):
        return parsed, False
    return [], True

def process_messages(messages):
    drinks = []
    processed = set()
    i = 0

    while i < len(messages):
        msg = messages[i]
        rowid, handle_id, text, date, has_attachment = msg

        if rowid in processed or is_reaction(text):
            i += 1
            continue

        # Case 1: number AND image in same message
        if has_attachment and text:
            num, details = parse_drink_text(text)
            if num:
                person = resolve_name(handle_id)
                dt = apple_ts_to_str(date)
                drinks.append({"drink_number": num, "person": person, "details": details, "date": dt, "imessage_id": rowid, "source": "auto"})
                print(f"  Logged (same msg): #{num} by {person}")
                processed.add(rowid)
                i += 1
                continue

        # Case 2: image message — look forward for number(s)
        if has_attachment:
            image_msg = msg
            image_sender = handle_id
            image_date = date
            parsed = []
            following_msgs = []

            j = i + 1
            lookahead = 0
            different_sender_num = None
            needs_math = False

            while j < len(messages) and lookahead < 5:
                next_msg = messages[j]
                next_rowid, next_handle, next_text, next_date, next_attachment = next_msg

                if is_reaction(next_text):
                    j += 1
                    continue

                if next_handle == image_sender:
                    # math request from same sender
                    if is_math_request(next_text or ""):
                        needs_math = True
                        following_msgs.append(next_msg)
                        processed.add(next_rowid)
                        j += 1
                        break

                    # range from same sender — use AI
                    if next_text and is_range(next_text):
                        before = messages[max(0, i-CONTEXT_WINDOW):i]
                        after = messages[j+1:j+1+CONTEXT_WINDOW]
                        ai_results = parse_ambiguous([image_msg, next_msg], "RANGE", before, after)
                        if ai_results:
                            dt = apple_ts_to_str(image_date)
                            for r in ai_results:
                                drinks.append({"drink_number": r["drink_number"], "person": r["person"], "details": r.get("details"), "date": dt, "imessage_id": next_rowid, "source": "ai"})
                                print(f"  Logged (AI/range): #{r['drink_number']} by {r['person']}")
                        else:
                            flag("RANGE", [image_msg, next_msg])
                        processed.add(next_rowid)
                        j += 1
                        break

                    num, details = parse_drink_text(next_text)
                    if num:
                        starred = "*" in (next_text or "")
                        parsed.append((num, details, next_date, next_rowid, starred))
                        processed.add(next_rowid)
                        following_msgs.append(next_msg)
                        j += 1
                        lookahead += 1
                        continue
                    elif next_attachment:
                        break
                    else:
                        j += 1
                        lookahead += 1
                        continue
                else:
                    # different sender — check if it's a number or math request
                    if next_text and not is_reaction(next_text):
                        if is_math_request(next_text):
                            needs_math = True
                            following_msgs.append(next_msg)
                            processed.add(next_rowid)
                            j += 1
                        elif not parsed:
                            num, _ = parse_drink_text(next_text)
                            if num:
                                different_sender_num = next_msg
                    break

            # handle math request — use AI with context
            if needs_math:
                before = messages[max(0, i-CONTEXT_WINDOW):i]
                after = messages[j:j+CONTEXT_WINDOW]
                ai_results = parse_ambiguous([image_msg] + following_msgs, "MATH_REQUEST", before, after)
                if ai_results:
                    dt = apple_ts_to_str(image_date)
                    for r in ai_results:
                        drinks.append({"drink_number": r["drink_number"], "person": r["person"], "details": r.get("details"), "date": dt, "imessage_id": rowid, "source": "ai"})
                        print(f"  Logged (AI/math): #{r['drink_number']} by {r['person']}")
                else:
                    flag("MATH_REQUEST", [image_msg] + following_msgs)

            # different sender typed the number — attribute to IMAGE sender, no AI needed
            elif different_sender_num and not parsed:
                num, details = parse_drink_text(different_sender_num[2])
                if num:
                    person = resolve_name(image_sender)
                    dt = apple_ts_to_str(image_date)
                    drinks.append({"drink_number": num, "person": person, "details": details, "date": dt, "imessage_id": different_sender_num[0], "source": "auto"})
                    print(f"  Logged (diff sender→img owner): #{num} by {person}")

            # same sender numbers — apply correction/sequential rules
            elif parsed:
                to_log, should_flag = resolve_drink_numbers(parsed)
                if should_flag:
                    # ambiguous — use AI
                    before = messages[max(0, i-CONTEXT_WINDOW):i]
                    after = messages[j:j+CONTEXT_WINDOW]
                    ai_results = parse_ambiguous([image_msg] + following_msgs, "AMBIGUOUS_MULTIPLE_NUMBERS", before, after)
                    if ai_results:
                        dt = apple_ts_to_str(image_date)
                        for r in ai_results:
                            drinks.append({"drink_number": r["drink_number"], "person": r["person"], "details": r.get("details"), "date": dt, "imessage_id": rowid, "source": "ai"})
                            print(f"  Logged (AI/ambiguous): #{r['drink_number']} by {r['person']}")
                    else:
                        flag("AMBIGUOUS_MULTIPLE_NUMBERS", [image_msg] + following_msgs)
                else:
                    person = resolve_name(image_sender)
                    dt = apple_ts_to_str(image_date)
                    for num, details, _, rid, _ in to_log:
                        drinks.append({"drink_number": num, "person": person, "details": details, "date": dt, "imessage_id": rid, "source": "auto"})
                        print(f"  Logged: #{num} by {person} ({details or ''})")

            processed.add(rowid)
            i = j
            continue

        # Case 3: number before image
        if text and not has_attachment:
            num, details = parse_drink_text(text)
            if num:
                j = i + 1
                while j < len(messages) and is_reaction(messages[j][2]):
                    j += 1
                if j < len(messages):
                    next_msg = messages[j]
                    next_rowid, next_handle, next_text, next_date, next_attachment = next_msg
                    if next_handle == handle_id and next_attachment:
                        person = resolve_name(handle_id)
                        dt = apple_ts_to_str(date)
                        drinks.append({"drink_number": num, "person": person, "details": details, "date": dt, "imessage_id": rowid, "source": "auto"})
                        print(f"  Logged (num before image): #{num} by {person}")
                        processed.add(rowid)
                        processed.add(next_rowid)
                        i = j + 1
                        continue

            if text and is_range(text):
                flag("RANGE", [msg])

        i += 1

    return drinks

def save_drinks(drinks_conn, drinks):
    for d in drinks:
        try:
            drinks_conn.execute("""
                INSERT OR IGNORE INTO drinks (drink_number, person, details, date, imessage_id, source)
                VALUES (:drink_number, :person, :details, :date, :imessage_id, :source)
            """, d)
        except Exception as e:
            print(f"  Error saving drink: {e}")
    drinks_conn.commit()

def run():
    drinks_conn = sqlite3.connect(DRINKS_DB)
    init_drinks_db(drinks_conn)
    print(f"Watcher started, polling every {POLL_INTERVAL}s...")

    while True:
        try:
            last_id = get_last_processed(drinks_conn)
            chat_conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
            messages = fetch_new_messages(chat_conn, last_id)
            chat_conn.close()

            if messages:
                drinks = process_messages(messages)
                if drinks:
                    save_drinks(drinks_conn, drinks)
                set_last_processed(drinks_conn, messages[-1][0])

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
