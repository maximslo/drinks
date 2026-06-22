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
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from parser import parse_numbers, resolve_name, PHONE_TO_NAME

CHAT_DB   = os.getenv("CHAT_DB_PATH",  os.path.expanduser("~/Library/Messages/chat.db"))
DRINKS_DB = os.getenv("DRINKS_DB_PATH", os.path.expanduser("~/drinks/data/drinks.db"))
MSGS_DB   = os.getenv("MSGS_DB_PATH",  os.path.expanduser("~/drinks/data/messages.db"))
DATA_DIR  = os.path.expanduser("~/drinks/data")
CHAT_ID   = os.getenv("CHAT_ID",        "chat313739884378608609")
SELF      = os.getenv("SELF_HANDLE",    "+17812050278")  # Mac Mini owner; handle_id is NULL for self-sent messages
POLL_INTERVAL = 2
PENDING_TIMEOUT = 90
CORRECTION_WINDOW = 1800  # 30 min: starred correction can fix a recently logged drink

_recently_resolved = {}  # handle_id → (drink_number, logged_at)


# ─── Per-sender pending state ─────────────────────────────────────────────────

@dataclass
class PendingLog:
    sender: str
    photos: list = field(default_factory=list)   # [(rowid, date), ...]
    numbers: list = field(default_factory=list)  # [(number, details, starred), ...]
    started_at: float = field(default_factory=time.time)
    raw_msgs: list = field(default_factory=list)

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
            imessage_id INTEGER,
            source TEXT DEFAULT 'auto'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(drinks)")}
    if "source" not in existing:
        conn.execute("ALTER TABLE drinks ADD COLUMN source TEXT DEFAULT 'auto'")
    # Drop UNIQUE from imessage_id if present (multiple drinks can share one photo message)
    has_imessage_unique = any(
        any(col[2] == 'imessage_id' for col in conn.execute(f"PRAGMA index_info('{idx[1]}')"))
        for idx in conn.execute("PRAGMA index_list(drinks)") if idx[2]
    )
    if has_imessage_unique:
        conn.execute("ALTER TABLE drinks RENAME TO _drinks_old")
        conn.execute("""
            CREATE TABLE drinks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                drink_number INTEGER UNIQUE,
                person TEXT,
                details TEXT,
                date TEXT,
                imessage_id INTEGER,
                source TEXT DEFAULT 'auto'
            )
        """)
        conn.execute("INSERT INTO drinks SELECT * FROM _drinks_old")
        conn.execute("DROP TABLE _drinks_old")
    conn.commit()

def get_last_processed(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='last_imessage_id'").fetchone()
    return int(row[0]) if row else 0

def set_last_processed(conn, imessage_id):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_imessage_id', ?)", (imessage_id,))
    conn.commit()

def get_last_drink_number(conn, person):
    row = conn.execute("SELECT MIN(drink_number) FROM drinks WHERE person = ?", (person,)).fetchone()
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

def apple_ts_to_str(date):
    apple_epoch = 978307200
    ts = apple_epoch + date / 1e9
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def is_consecutive(nums):
    """nums must be sorted descending."""
    return all(nums[i] - nums[i + 1] == 1 for i in range(len(nums) - 1))


# ─── Flagging ─────────────────────────────────────────────────────────────────

def flag(reason, messages):
    print(f"\n[FLAGGED: {reason}]")
    for m in messages:
        print(f"  ROWID={m[0]} handle={m[1]} text={m[2]!r}")


# ─── Face recognition ─────────────────────────────────────────────────────────

_name_lower_to_canonical = {v.lower(): v for v in PHONE_TO_NAME.values()}

def _canonical_name(name):
    """Map a face-recognized name to the canonical name used in drinks.db."""
    return _name_lower_to_canonical.get(name.lower(), name)


_recognize_fn = None

def _get_recognize():
    global _recognize_fn
    if _recognize_fn is None:
        try:
            from facial.recognize import recognize
            _recognize_fn = recognize
            print("  [FACES] recognition model loaded")
        except Exception as e:
            print(f"  [FACES] recognize unavailable: {e}")
            _recognize_fn = lambda path: []
    return _recognize_fn

def identify_faces(photo_rowids):
    """Return unique high-confidence names across all photos, sorted by confidence desc."""
    recognize = _get_recognize()
    seen = {}
    try:
        msgs_conn = sqlite3.connect(f"file:{MSGS_DB}?mode=ro", uri=True)
        chat_conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        try:
            for rowid in photo_rowids:
                # Try synced messages.db first, fall back to chat.db attachment table
                row = msgs_conn.execute(
                    "SELECT attachment_path FROM messages WHERE rowid=?", (rowid,)
                ).fetchone()
                if row and row[0]:
                    path = os.path.join(DATA_DIR, row[0])
                else:
                    chat_row = chat_conn.execute("""
                        SELECT a.filename FROM attachment a
                        JOIN message_attachment_join maj ON a.ROWID = maj.attachment_id
                        WHERE maj.message_id = ?
                    """, (rowid,)).fetchone()
                    if not chat_row or not chat_row[0]:
                        print(f"  [FACES] rowid={rowid} → no attachment found in messages.db or chat.db")
                        continue
                    path = os.path.expanduser(chat_row[0])
                if not os.path.exists(path):
                    print(f"  [FACES] rowid={rowid} → file not found: {path}")
                    continue
                raw = recognize(path)
                print(f"  [FACES] rowid={rowid} → {len(raw)} face(s) detected in {os.path.basename(path)}")
                for r in raw:
                    raw_name = r["name"]
                    canonical = _canonical_name(raw_name)
                    is_member = canonical in _name_lower_to_canonical.values()
                    print(f"    cluster={raw_name!r} → {canonical!r} conf={r['confidence']:.3f} member={is_member}")
                    if is_member and r["confidence"] > seen.get(canonical, 0):
                        seen[canonical] = r["confidence"]
        finally:
            chat_conn.close()
            msgs_conn.close()
    except Exception as e:
        print(f"  [FACES] error: {e}")
    names = [n for n, _ in sorted(seen.items(), key=lambda x: -x[1])]
    print(f"  [FACES] assigned candidates: {names if names else 'none (all → sender)'}")
    return names

def _assign_drinks(sorted_nums, numbers, sender_name, face_names):
    """Greedy: one drink per identified face (highest confidence first), leftovers → sender."""
    details_map = {n: d for n, d, _ in numbers}
    unassigned = list(sorted_nums)
    result = []
    for name in face_names:
        if not unassigned:
            break
        num = unassigned.pop(0)
        result.append((num, name, details_map.get(num)))
    for num in unassigned:
        result.append((num, sender_name, details_map.get(num)))
    return sorted(result, key=lambda x: -x[0])


# ─── Core logic ───────────────────────────────────────────────────────────────

def save_drink(conn, drink_number, person, details, date, imessage_id, source):
    try:
        cur = conn.execute("""
            INSERT OR IGNORE INTO drinks (drink_number, person, details, date, imessage_id, source)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (drink_number, person, details, date, imessage_id, source))
        conn.commit()
        if cur.rowcount > 0:
            print(f"  Logged: #{drink_number} by {person}{' — ' + details if details else ''} [{source}]")
        else:
            print(f"  Skipped: #{drink_number} already logged")
    except Exception as e:
        print(f"  Error saving #{drink_number}: {e}")

def _finish(sender, logged_nums):
    """After logging, record what was logged for corrections and close pending."""
    nums = frozenset([logged_nums] if isinstance(logged_nums, int) else logged_nums)
    pending.pop(sender, None)
    _recently_resolved[sender] = (nums, time.time())

def try_resolve(sender, drinks_conn):
    """Attempt to log drinks for sender if we have both photo and number(s)."""
    p = pending.get(sender)
    if not p or not p.photos:
        return
    if not p.numbers:
        return

    photo_rowid, photo_date = p.photos[0]
    person = resolve_name(sender)
    dt = apple_ts_to_str(photo_date)

    # Starred = correction
    starred = [(n, d, s) for n, d, s in p.numbers if s]
    if starred:
        new_endpoint, details, _ = starred[-1]
        non_starred = [n for n, _, s in p.numbers if not s]
        last = get_last_drink_number(drinks_conn, person)

        if non_starred and last is not None:
            # Bad range never logged — reconstruct from last-1 down to endpoint
            start = last - 1
            span = start - new_endpoint
            if 1 <= span <= 19:
                print(f"  [CORRECTION] reconstructing #{start}→#{new_endpoint} for {person} ({span + 1} drinks)")
                range_nums = list(range(start, new_endpoint - 1, -1))
                for i, n in enumerate(range_nums):
                    d = details if i == len(range_nums) - 1 else None
                    save_drink(drinks_conn, n, person, d, dt, photo_rowid, "auto")
                _finish(sender, set(range_nums))
                return

        save_drink(drinks_conn, new_endpoint, person, details, dt, photo_rowid, "auto")
        _finish(sender, {new_endpoint})
        return

    if len(p.numbers) == 1:
        num, details, _ = p.numbers[0]
        save_drink(drinks_conn, num, person, details, dt, photo_rowid, "auto")
        _finish(sender, num)
        return

    # Multiple numbers — consecutive range logs immediately; non-consecutive waits for *correction
    sorted_nums = sorted([n for n, _, _ in p.numbers], reverse=True)
    if is_consecutive(sorted_nums):
        face_names = identify_faces([r for r, _ in p.photos])
        assignments = _assign_drinks(sorted_nums, p.numbers, person, face_names)
        for num, assigned_person, details in assignments:
            save_drink(drinks_conn, num, assigned_person, details, dt, photo_rowid, "auto")
        _finish(sender, {n for n, _, _ in p.numbers})
        return

    print(f"  [WAIT] {person}: non-consecutive {sorted_nums} — waiting for *correction")

def _pending_summary():
    if not pending:
        return "pending=none"
    parts = []
    for h, p in pending.items():
        parts.append(f"{resolve_name(h)}:{'📷' if p.photos else ''}{'#' if p.numbers else ''}")
    return "pending=[" + " ".join(parts) + "]"

def handle_message(msg, drinks_conn):
    rowid, handle_id, text, date, has_attachment = msg
    if handle_id is None:
        handle_id = SELF

    clean = (text or "").lstrip("￼").strip()  # strip iMessage attachment placeholder

    if is_reaction(clean):
        return

    numbers = parse_numbers(clean)
    person = resolve_name(handle_id)

    attach_str = "attach=yes" if has_attachment else "attach=no"
    nums_str = f"parsed={[n for n,_,_ in numbers]}" if numbers else "parsed=[]"
    print(f"  [MSG] {person} | {attach_str} | text={clean!r} | {nums_str}")

    # Case 1: photo + number(s) in same message — log immediately
    if has_attachment and numbers:
        print(f"  [CASE1] photo+number in same msg → logging immediately")
        dt = apple_ts_to_str(date)
        for num, details, _ in numbers:
            save_drink(drinks_conn, num, person, details, dt, rowid, "auto")
        _recently_resolved[handle_id] = (min(n for n, _, _ in numbers), time.time())
        return

    # Case 2: photo only — open/extend pending for this sender
    if has_attachment:
        row = drinks_conn.execute("SELECT MIN(drink_number) FROM drinks").fetchone()
        last = row[0] if row and row[0] is not None else None
        expected = f"#{last - 1}" if last else "any (no history)"
        print(f"  [CASE2] photo only → waiting for number from {person} | next expected: {expected}")
        if handle_id not in pending:
            pending[handle_id] = PendingLog(sender=handle_id)
        pending[handle_id].photos.append((rowid, date))
        pending[handle_id].raw_msgs.append(msg)
        return

    # Case 3: numbers only number(s) only
    if numbers:
        # Starred correction within window → fix recently logged drink(s)
        starred = [(n, d) for n, d, s in numbers if s]
        if starred and handle_id not in pending:
            recent = _recently_resolved.get(handle_id)
            if recent:
                old_nums, logged_at = recent
                if time.time() - logged_at < CORRECTION_WINDOW:
                    new_num, new_details = starred[-1]
                    non_starred_new = [n for n, _, s in numbers if not s]
                    if non_starred_new:
                        # Multi-drink correction: fetch old photo info, delete old, insert new
                        row = drinks_conn.execute(
                            "SELECT imessage_id, date FROM drinks WHERE drink_number=? AND person=?",
                            (min(old_nums), person)
                        ).fetchone()
                        old_imessage_id = row[0] if row else None
                        old_date = row[1] if row else apple_ts_to_str(date)
                        for old_n in old_nums:
                            drinks_conn.execute("DELETE FROM drinks WHERE drink_number=? AND person=?", (old_n, person))
                        drinks_conn.commit()
                        new_ns = sorted((n for n, _, _ in numbers), reverse=True)
                        print(f"  [CORRECTION] {sorted(old_nums, reverse=True)} → {new_ns} for {person}")
                        for n, d, _ in sorted(numbers, key=lambda x: x[0], reverse=True):
                            save_drink(drinks_conn, n, person, d, old_date, old_imessage_id, "auto")
                        _recently_resolved[handle_id] = (frozenset(n for n, _, _ in numbers), time.time())
                    else:
                        # Single drink correction: update in-place
                        old_num = min(old_nums)
                        drinks_conn.execute(
                            "UPDATE drinks SET drink_number=?" + (", details=?" if new_details else "") + " WHERE drink_number=? AND person=?",
                            ([new_num, new_details, old_num, person] if new_details else [new_num, old_num, person])
                        )
                        drinks_conn.commit()
                        _recently_resolved[handle_id] = (frozenset([new_num]), time.time())
                        print(f"  [CORRECTION] #{old_num} → #{new_num} for {person}")
                    return

        # This sender already has a pending photo → pair with it
        if handle_id in pending and pending[handle_id].photos:
            print(f"  [CASE4] {person} has pending photo → pairing")
            pending[handle_id].numbers.extend(numbers)
            pending[handle_id].raw_msgs.append(msg)
            return

        # Another sender has a pending photo → attribute to them
        for photo_sender, p in pending.items():
            if photo_sender != handle_id and p.photos and not p.numbers:
                photo_person = resolve_name(photo_sender)
                print(f"  [CASE4] attributing to {photo_person}'s pending photo")
                p.numbers.extend(numbers)
                p.raw_msgs.append(msg)
                return

        # No pending photo — check sequence then open a window
        row = drinks_conn.execute("SELECT MIN(drink_number) FROM drinks").fetchone()
        last = row[0] if row and row[0] is not None else None
        sorted_nums = sorted([n for n, _, _ in numbers], reverse=True)
        starred = any(s for _, _, s in numbers)
        if last is not None and not starred:
            if sorted_nums[0] != last - 1 or not is_consecutive(sorted_nums):
                print(f"  [SKIP] expected #{last - 1}, got #{sorted_nums[0]} — not next in sequence")
                return
        print(f"  [CASE4] no pending photo | got={sorted_nums} → waiting for photo")
        if handle_id not in pending:
            pending[handle_id] = PendingLog(sender=handle_id)
        pending[handle_id].numbers.extend(numbers)
        pending[handle_id].raw_msgs.append(msg)

    if not numbers and not has_attachment:
        print(f"  [SKIP] no numbers, no photo | {_pending_summary()}")

def check_expirations(drinks_conn):
    now = time.time()
    expired = [s for s, p in pending.items() if now - p.started_at > PENDING_TIMEOUT]
    for sender in expired:
        p = pending.pop(sender)
        if p.photos and not p.numbers:
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
            # Resolve after full batch so same-tick corrections (e.g. 5583 then 5593*) are seen together
            for sender in list(pending):
                p = pending.get(sender)
                if p and p.photos:
                    try_resolve(sender, drinks_conn)
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
    print(f"DRINKS_DB: {DRINKS_DB}")
    print(f"CHAT_DB:   {CHAT_DB}")
    print(f"CHAT_ID:   {CHAT_ID}")
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
