import sqlite3
import time
import os
import sys
import re
import plistlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '../.env'))

sys.path.insert(0, os.path.dirname(__file__))
from parser import parse_numbers, resolve_name
from ai_parser import parse_ambiguous

CHAT_DB   = os.getenv("CHAT_DB_PATH",  os.path.expanduser("~/Library/Messages/chat.db"))
DRINKS_DB = os.getenv("DRINKS_DB_PATH", os.path.expanduser("~/drinks/data/drinks.db"))
CHAT_ID   = os.getenv("CHAT_ID",        "chat313739884378608609")
FLAG_LOG  = os.path.expanduser("~/drinks/data/flagged.log")
SELF      = os.getenv("SELF_HANDLE",    "+17812050278")  # Mac Mini owner; handle_id is NULL for self-sent messages
POLL_INTERVAL = 2
PENDING_TIMEOUT = 90


# ─── Per-sender pending state ─────────────────────────────────────────────────

@dataclass
class PendingLog:
    sender: str
    photos: list = field(default_factory=list)   # [(rowid, date), ...]
    numbers: list = field(default_factory=list)  # [(number, details, starred), ...]
    started_at: float = field(default_factory=time.time)
    raw_msgs: list = field(default_factory=list)
    needs_math: bool = False

pending: dict = {}  # handle_id → PendingLog


# ─── drinks.db helpers ────────────────────────────────────────────────────────

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
    # Add source column to existing tables that predate it
    existing = {row[1] for row in conn.execute("PRAGMA table_info(drinks)")}
    if "source" not in existing:
        conn.execute("ALTER TABLE drinks ADD COLUMN source TEXT DEFAULT 'auto'")
    conn.commit()

def get_last_processed(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='last_imessage_id'").fetchone()
    return int(row[0]) if row else 0

def set_last_processed(conn, imessage_id):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_imessage_id', ?)", (imessage_id,))
    conn.commit()

def get_last_drink_number(conn, person):
    row = conn.execute("SELECT MAX(drink_number) FROM drinks WHERE person = ?", (person,)).fetchone()
    return row[0] if row and row[0] is not None else None


# ─── chat.db helpers ──────────────────────────────────────────────────────────

_ATTRIBUTED_BODY_SKIP = re.compile(
    r'^(streamtyped|NS[A-Za-z]+|__kIM[A-Za-z]+|__k[A-Za-z]+|NSDictionary|NSValue|NSNumber)$'
)

def _text_from_attributed_body(blob):
    """Extract plain text from a typedstream-encoded NSAttributedString blob."""
    if not blob:
        return None
    data = bytes(blob)
    # Find all printable ASCII sequences, skip framework/class name tokens
    for seq in re.findall(rb'[ -~]{2,}', data):
        try:
            s = seq.decode('utf-8').strip()
        except UnicodeDecodeError:
            continue
        if s and not _ATTRIBUTED_BODY_SKIP.match(s) and not s.startswith(('$', '&', '"')):
            return s
    return None

def fetch_new_messages(chat_conn, last_id):
    rows = chat_conn.execute("""
        SELECT
            message.ROWID,
            handle.id as handle_id,
            message.is_from_me,
            message.text,
            message.attributedBody,
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

    normalised = []
    for rowid, handle_id, is_from_me, text, attributed_body, date, has_attachment in rows:
        if is_from_me:
            handle_id = SELF
        if not text:
            text = _text_from_attributed_body(attributed_body)
        normalised.append((rowid, handle_id, text, date, has_attachment))
    return normalised


# ─── Utilities ────────────────────────────────────────────────────────────────

def is_reaction(text):
    if not text:
        return False
    prefixes = ["loved", "liked", "disliked", "laughed at", "emphasized", "questioned", "reacted"]
    return any(text.lower().startswith(p) for p in prefixes)

def is_math_request(text):
    if not text:
        return False
    keywords = ["do the math", "someone count", "figure it out", "math pls", "math please"]
    return any(k in text.lower() for k in keywords)

def apple_ts_to_str(date):
    apple_epoch = 978307200
    ts = apple_epoch + date / 1e9
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def is_consecutive(nums):
    """nums must be sorted descending."""
    return all(nums[i] - nums[i + 1] == 1 for i in range(len(nums) - 1))

def is_plausible(numbers, person, conn):
    """True if these drink numbers could plausibly be this person's next log."""
    last = get_last_drink_number(conn, person)
    if last is None:
        return True  # no history — accept anything
    if any(starred for _, _, starred in numbers):
        return True  # explicit correction
    sorted_nums = sorted([n for n, _, _ in numbers], reverse=True)
    expected = last + len(numbers)
    return sorted_nums[0] == expected and is_consecutive(sorted_nums)


# ─── Flagging ─────────────────────────────────────────────────────────────────

def flag(reason, messages):
    with open(FLAG_LOG, "a") as f:
        f.write(f"\n[FLAGGED: {reason}]\n")
        for m in messages:
            f.write(f"  ROWID={m[0]} handle={m[1]} text={m[2]!r}\n")


# ─── Core logic ───────────────────────────────────────────────────────────────

def save_drink(conn, drink_number, person, details, date, imessage_id, source):
    try:
        conn.execute("""
            INSERT OR IGNORE INTO drinks (drink_number, person, details, date, imessage_id, source)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (drink_number, person, details, date, imessage_id, source))
        conn.commit()
        print(f"  Logged: #{drink_number} by {person}{' — ' + details if details else ''} [{source}]")
    except Exception as e:
        print(f"  Error saving #{drink_number}: {e}")

def try_resolve(sender, drinks_conn):
    """Attempt to log drinks for sender if we have both photo and number(s)."""
    p = pending.get(sender)
    if not p or not p.photos:
        return
    if not p.numbers and not p.needs_math:
        return

    photo_rowid, photo_date = p.photos[0]
    person = resolve_name(sender)
    dt = apple_ts_to_str(photo_date)

    if p.needs_math:
        ai_results = parse_ambiguous(p.raw_msgs, "MATH_REQUEST")
        if ai_results:
            for r in ai_results:
                save_drink(drinks_conn, r["drink_number"], r["person"], r.get("details"), dt, photo_rowid, "ai")
        else:
            flag("MATH_REQUEST", p.raw_msgs)
        del pending[sender]
        return

    # Starred = correction override — use the last starred number
    starred = [(n, d, s) for n, d, s in p.numbers if s]
    if starred:
        num, details, _ = starred[-1]
        save_drink(drinks_conn, num, person, details, dt, photo_rowid, "auto")
        del pending[sender]
        return

    if len(p.numbers) == 1:
        num, details, _ = p.numbers[0]
        save_drink(drinks_conn, num, person, details, dt, photo_rowid, "auto")
        del pending[sender]
        return

    # Multiple numbers — consecutive range goes to same person, otherwise AI
    sorted_nums = sorted([n for n, _, _ in p.numbers], reverse=True)
    if is_consecutive(sorted_nums):
        for num, details, _ in sorted(p.numbers, key=lambda x: x[0], reverse=True):
            save_drink(drinks_conn, num, person, details, dt, photo_rowid, "auto")
        del pending[sender]
        return

    ai_results = parse_ambiguous(p.raw_msgs, "AMBIGUOUS_MULTIPLE_NUMBERS")
    if ai_results:
        for r in ai_results:
            save_drink(drinks_conn, r["drink_number"], r["person"], r.get("details"), dt, photo_rowid, "ai")
    else:
        flag("AMBIGUOUS_MULTIPLE_NUMBERS", p.raw_msgs)
    del pending[sender]

def handle_message(msg, drinks_conn):
    rowid, handle_id, text, date, has_attachment = msg
    if handle_id is None:
        handle_id = SELF

    if is_reaction(text):
        return

    clean = (text or "").lstrip("￼").strip()  # strip iMessage attachment placeholder
    numbers = parse_numbers(clean)

    # Case 1: photo + number(s) in same message — log immediately
    if has_attachment and numbers:
        person = resolve_name(handle_id)
        dt = apple_ts_to_str(date)
        for num, details, _ in numbers:
            save_drink(drinks_conn, num, person, details, dt, rowid, "auto")
        return

    # Case 2: photo only — open/extend pending for this sender
    if has_attachment:
        if handle_id not in pending:
            pending[handle_id] = PendingLog(sender=handle_id)
        pending[handle_id].photos.append((rowid, date))
        pending[handle_id].raw_msgs.append(msg)
        try_resolve(handle_id, drinks_conn)
        return

    # Case 3: math request — flag pending for AI
    if is_math_request(clean):
        if handle_id in pending:
            pending[handle_id].needs_math = True
            pending[handle_id].raw_msgs.append(msg)
            try_resolve(handle_id, drinks_conn)
        return

    # Case 4: number(s) only
    if numbers:
        person = resolve_name(handle_id)

        # This sender already has a pending photo → pair with it
        if handle_id in pending and pending[handle_id].photos:
            pending[handle_id].numbers.extend(numbers)
            pending[handle_id].raw_msgs.append(msg)
            try_resolve(handle_id, drinks_conn)
            return

        # Another sender has a pending photo and this number matches them → attribute to photo sender
        for photo_sender, p in pending.items():
            if photo_sender != handle_id and p.photos and not p.numbers:
                photo_person = resolve_name(photo_sender)
                if is_plausible(numbers, photo_person, drinks_conn):
                    p.numbers.extend(numbers)
                    p.raw_msgs.append(msg)
                    try_resolve(photo_sender, drinks_conn)
                    return

        # No pending photo anywhere — validate before opening a pending window for this sender
        if is_plausible(numbers, person, drinks_conn):
            if handle_id not in pending:
                pending[handle_id] = PendingLog(sender=handle_id)
            pending[handle_id].numbers.extend(numbers)
            pending[handle_id].raw_msgs.append(msg)
            return  # waiting for photo
        # Truly unrelated — skip

def check_expirations(drinks_conn):
    now = time.time()
    expired = [s for s, p in pending.items() if now - p.started_at > PENDING_TIMEOUT]
    for sender in expired:
        p = pending.pop(sender)
        if p.photos and not p.numbers and not p.needs_math:
            pass  # random chat photo, discard silently
        elif p.numbers and not p.photos:
            flag("MISSING_PHOTO", p.raw_msgs)
            print(f"  Expired: {resolve_name(sender)} sent number without photo")
        else:
            flag("UNRESOLVED", p.raw_msgs)
            print(f"  Expired: unresolved log from {resolve_name(sender)}")


# ─── Poll loop ────────────────────────────────────────────────────────────────

def check_new_messages():
    try:
        drinks_conn = sqlite3.connect(DRINKS_DB)
        init_drinks_db(drinks_conn)
        last_id = get_last_processed(drinks_conn)

        chat_conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        messages = fetch_new_messages(chat_conn, last_id)
        chat_conn.close()

        if messages:
            print(f"  {len(messages)} new message(s)")
            for msg in messages:
                handle_message(msg, drinks_conn)
            check_expirations(drinks_conn)
            set_last_processed(drinks_conn, messages[-1][0])

        drinks_conn.close()
    except Exception as e:
        print(f"Error: {e}")

def init_cursor():
    drinks_conn = sqlite3.connect(DRINKS_DB)
    init_drinks_db(drinks_conn)
    last_id = get_last_processed(drinks_conn)
    print(f"Starting from ROWID {last_id}")
    drinks_conn.close()

def run():
    init_cursor()
    print(f"Watcher started (polling every {POLL_INTERVAL}s)")
    try:
        while True:
            time.sleep(POLL_INTERVAL)
            check_new_messages()
    except KeyboardInterrupt:
        print("Watcher stopped.")

if __name__ == "__main__":
    run()
