import sqlite3
import argparse
import os
import shutil
import subprocess
from datetime import datetime, timezone

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
MESSAGES_DB = os.path.expanduser("~/drinks/data/messages.db")
ATTACHMENTS_DIR = os.path.expanduser("~/drinks/data/attachments")
CHAT_IDS = [
    "chat313739884378608609",   # main / current
    "chat247636595391927399",   # OG
    "chat26176758262309627",    # OG (oldest)
]

APPLE_EPOCH = 978307200
REACTION_PREFIXES = ("loved", "liked", "disliked", "laughed at", "emphasized", "questioned", "reacted")
CHUNK = 500  # max SQLite IN-clause params


def apple_date_to_iso(date_val):
    ts = APPLE_EPOCH + (date_val / 1_000_000_000 if date_val > 1_000_000_000_000 else date_val)
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def is_reaction(text):
    if not text:
        return False
    return any(text.lower().startswith(p) for p in REACTION_PREFIXES)


def parse_attributed_body(blob: bytes):
    """Extract plain text from an NSAttributedString typedstream blob.

    In macOS Ventura+, iMessage stores message text in attributedBody (a
    typedstream-encoded NSAttributedString) rather than the text column.
    The plain string content is stored as \x01 <length> <utf8-bytes> where
    length is a single byte for strings ≤ 127 chars, or \x82 <hi> <lo> for longer.
    """
    if not blob:
        return None
    try:
        start = blob.find(b'streamtyped')
        if start < 0:
            return None
        i = start + len('streamtyped')

        while i < len(blob):
            pos = blob.find(b'\x01', i)
            if pos < 0:
                break
            pos += 1  # skip \x01

            if pos >= len(blob):
                break

            length_byte = blob[pos]
            if length_byte == 0x2b:
                # macOS Sequoia+ format: \x01 \x2b <length> <content>
                if pos + 1 >= len(blob):
                    i = pos
                    continue
                next_byte = blob[pos + 1]
                if next_byte == 0x82:
                    if pos + 4 > len(blob):
                        i = pos
                        continue
                    length = (blob[pos + 2] << 8) | blob[pos + 3]
                    data_start = pos + 4
                elif 0 < next_byte < 0x82:
                    length = next_byte
                    data_start = pos + 2
                else:
                    i = pos
                    continue
            elif length_byte == 0x82:
                if pos + 3 > len(blob):
                    i = pos
                    continue
                length = (blob[pos + 1] << 8) | blob[pos + 2]
                data_start = pos + 3
            elif 0 < length_byte < 0x82:
                length = length_byte
                data_start = pos + 1
            else:
                i = pos
                continue

            if data_start + length > len(blob):
                i = pos
                continue

            try:
                text = blob[data_start:data_start + length].decode('utf-8')
                if text.strip():
                    return text
            except UnicodeDecodeError:
                i = pos  # don't skip past the real string on a bad decode
                continue

            i = data_start + max(1, length)

    except Exception:
        pass
    return None


def resolve_text(raw_text, attributed_body):
    """Return displayable text, falling back to attributedBody parsing."""
    text = raw_text or parse_attributed_body(attributed_body)
    if text:
        text = text.lstrip('￼').strip() or None
    return text


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            rowid          INTEGER PRIMARY KEY,
            phone          TEXT,
            text           TEXT,
            sent_at        TEXT NOT NULL,
            is_from_me     INTEGER NOT NULL DEFAULT 0,
            has_attachment INTEGER NOT NULL DEFAULT 0,
            is_reaction    INTEGER NOT NULL DEFAULT 0,
            attachment_path TEXT,
            chat_id        TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attachments (
            message_rowid INTEGER NOT NULL,
            idx           INTEGER NOT NULL,
            path          TEXT,
            mime          TEXT,
            PRIMARY KEY (message_rowid, idx)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attach_msg ON attachments(message_rowid)")
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN attachment_path TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN chat_id TEXT")
        conn.execute("UPDATE messages SET chat_id = 'chat313739884378608609' WHERE chat_id IS NULL")
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_id ON messages(chat_id)")
    # migrate old global meta key to per-chat key
    old = conn.execute("SELECT value FROM meta WHERE key='last_synced_rowid'").fetchone()
    if old:
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            ("last_synced_rowid_chat313739884378608609", old[0])
        )
        conn.execute("DELETE FROM meta WHERE key='last_synced_rowid'")
    conn.commit()


def get_last_rowid(conn, chat_id):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (f"last_synced_rowid_{chat_id}",)).fetchone()
    return int(row[0]) if row else 0


def set_last_rowid(conn, chat_id, rowid):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (f"last_synced_rowid_{chat_id}", rowid))
    conn.commit()


def fetch_from_chat_db(chat_conn, last_rowid, chat_id):
    return chat_conn.execute("""
        SELECT
            m.ROWID,
            h.id AS phone,
            m.text,
            m.attributedBody,
            m.date,
            m.is_from_me,
            m.cache_has_attachments,
            a.filename   AS attach_filename,
            a.mime_type  AS attach_mime
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        LEFT JOIN message_attachment_join maj ON m.ROWID = maj.message_id
        LEFT JOIN attachment a ON maj.attachment_id = a.ROWID
            AND (a.mime_type LIKE 'image/%' OR a.mime_type LIKE 'video/%')
        WHERE c.chat_identifier = ?
          AND m.ROWID > ?
        ORDER BY m.ROWID ASC, a.ROWID ASC
    """, (chat_id, last_rowid)).fetchall()


def group_by_message(rows):
    """Collapse flat (message × attachment) rows into one entry per message.

    The chat.db query returns one row per attachment, so a message with N
    photos appears N times. Preserve ROWID order and collect ALL image/video
    attachments per message instead of keeping only the first.
    """
    groups = {}
    order = []
    for row in rows:
        rowid = row[0]
        if rowid not in groups:
            groups[rowid] = {"msg": row, "attachments": []}
            order.append(rowid)
        if row[7]:  # row[7] = attach_filename, row[8] = attach_mime
            groups[rowid]["attachments"].append((row[7], row[8]))
    return [groups[r] for r in order]


def copy_attachment(rowid, src_path, idx=0):
    """Copy a single attachment to data/attachments/.

    idx 0 keeps the legacy filename ``{rowid}.{ext}`` so existing files and the
    ``/attachment/{rowid}`` endpoint stay valid; idx > 0 uses ``{rowid}_{idx}.{ext}``.
    """
    if not src_path:
        return None
    src = os.path.expanduser(src_path)
    if not os.path.exists(src):
        return None
    suffix = "" if idx == 0 else f"_{idx}"
    ext = src_path.rsplit('.', 1)[-1].lower() if '.' in src_path else 'jpg'
    if ext == 'heic':
        dst = os.path.join(ATTACHMENTS_DIR, f"{rowid}{suffix}.jpg")
        if not os.path.exists(dst):
            r = subprocess.run(
                ['sips', '-s', 'format', 'jpeg', src, '--out', dst],
                capture_output=True
            )
            if r.returncode != 0 or not os.path.exists(dst):
                return None
        return f"attachments/{rowid}{suffix}.jpg"
    dst = os.path.join(ATTACHMENTS_DIR, f"{rowid}{suffix}.{ext}")
    if os.path.exists(dst):
        return f"attachments/{rowid}{suffix}.{ext}"
    shutil.copy2(src, dst)
    return f"attachments/{rowid}{suffix}.{ext}"


def fix_heic(msg_conn, verbose):
    """Convert existing .heic files to .jpg and update DB paths."""
    rows = msg_conn.execute(
        "SELECT rowid FROM messages WHERE attachment_path LIKE '%.heic'"
    ).fetchall()
    count = 0
    for (rowid,) in rows:
        src = os.path.join(ATTACHMENTS_DIR, f"{rowid}.heic")
        dst = os.path.join(ATTACHMENTS_DIR, f"{rowid}.jpg")
        if not os.path.exists(src):
            continue
        if not os.path.exists(dst):
            r = subprocess.run(
                ['sips', '-s', 'format', 'jpeg', src, '--out', dst],
                capture_output=True
            )
            if r.returncode != 0 or not os.path.exists(dst):
                continue
        msg_conn.execute(
            "UPDATE messages SET attachment_path = ? WHERE rowid = ?",
            (f"attachments/{rowid}.jpg", rowid)
        )
        count += 1
        if verbose:
            print(f"  Converted ROWID={rowid}")
    msg_conn.commit()
    return count


def upsert_messages(msg_conn, groups, chat_id, verbose):
    count = 0
    for g in groups:
        rowid, phone, raw_text, attributed_body, date_val, is_from_me, has_attachment, _f, _m = g["msg"]
        sent_at = apple_date_to_iso(date_val)
        text = resolve_text(raw_text, attributed_body)
        reaction = 1 if is_reaction(text) else 0

        # copy every image/video attachment for this message
        copied = []
        for idx, (filename, mime) in enumerate(g["attachments"]):
            path = copy_attachment(rowid, filename, idx)
            if path:
                copied.append((idx, path, mime))

        first_path = copied[0][1] if copied else None
        msg_conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(rowid, phone, text, sent_at, is_from_me, has_attachment, is_reaction, attachment_path, chat_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rowid, phone, text, sent_at, is_from_me or 0, has_attachment or 0, reaction, first_path, chat_id)
        )
        for idx, path, mime in copied:
            msg_conn.execute(
                "INSERT OR IGNORE INTO attachments (message_rowid, idx, path, mime) VALUES (?, ?, ?, ?)",
                (rowid, idx, path, mime)
            )
        count += 1
        if verbose:
            print(f"  ROWID={rowid} phone={phone} sent_at={sent_at} reaction={reaction} attach={len(copied)} text={str(text)[:50]!r}")
    msg_conn.commit()
    return count


def backfill_missing_text(msg_conn, chat_conn, verbose):
    """Parse attributedBody for existing rows that have NULL text."""
    null_rows = msg_conn.execute(
        "SELECT rowid FROM messages WHERE text IS NULL"
    ).fetchall()
    if not null_rows:
        return 0

    rowids = [r[0] for r in null_rows]
    count = 0

    for i in range(0, len(rowids), CHUNK):
        chunk = rowids[i:i + CHUNK]
        placeholders = ','.join('?' * len(chunk))
        ab_rows = chat_conn.execute(
            f"SELECT ROWID, text, attributedBody FROM message WHERE ROWID IN ({placeholders})",
            chunk
        ).fetchall()

        for rowid, raw_text, attributed_body in ab_rows:
            text = resolve_text(raw_text, attributed_body)
            if text:
                reaction = 1 if is_reaction(text) else 0
                msg_conn.execute(
                    "UPDATE messages SET text = ?, is_reaction = ? WHERE rowid = ?",
                    (text, reaction, rowid)
                )
                count += 1
                if verbose:
                    print(f"  Text backfill ROWID={rowid} reaction={reaction}: {text[:50]!r}")
        msg_conn.commit()

    return count


def fix_reaction_flags(msg_conn, verbose):
    """Fix is_reaction=0 rows whose text was backfilled but flag wasn't set."""
    rows = msg_conn.execute(
        "SELECT rowid, text FROM messages WHERE text IS NOT NULL AND is_reaction = 0"
    ).fetchall()
    count = 0
    for rowid, text in rows:
        if is_reaction(text):
            msg_conn.execute("UPDATE messages SET is_reaction = 1 WHERE rowid = ?", (rowid,))
            count += 1
            if verbose:
                print(f"  Fixed reaction flag ROWID={rowid}: {text[:50]!r}")
    msg_conn.commit()
    return count


def backfill_missing_attachments(msg_conn, chat_conn, verbose):
    """Copy attachments for rows already in messages.db that have no attachment_path yet."""
    rows_needing_attach = msg_conn.execute(
        "SELECT rowid FROM messages WHERE has_attachment = 1 AND attachment_path IS NULL"
    ).fetchall()
    if not rows_needing_attach:
        return 0

    rowids = [r[0] for r in rows_needing_attach]
    count = 0

    for i in range(0, len(rowids), CHUNK):
        chunk = rowids[i:i + CHUNK]
        placeholders = ','.join('?' * len(chunk))
        attach_rows = chat_conn.execute(f"""
            SELECT maj.message_id, a.filename
            FROM message_attachment_join maj
            JOIN attachment a ON maj.attachment_id = a.ROWID
            WHERE maj.message_id IN ({placeholders})
              AND (a.mime_type LIKE 'image/%' OR a.mime_type LIKE 'video/%')
            ORDER BY maj.message_id ASC, a.ROWID ASC
        """, chunk).fetchall()

        first_attach = {}
        for message_id, filename in attach_rows:
            if message_id not in first_attach:
                first_attach[message_id] = filename

        for message_id, src_path in first_attach.items():
            path = copy_attachment(message_id, src_path)
            if path:
                msg_conn.execute(
                    "UPDATE messages SET attachment_path = ? WHERE rowid = ?",
                    (path, message_id)
                )
                count += 1
                if verbose:
                    print(f"  Backfilled attachment ROWID={message_id} → {path}")
        msg_conn.commit()

    return count


def rebuild_attachments(msg_conn, chat_conn, verbose):
    """Populate the attachments table for every message with media, copying
    ALL image/video attachments per message (not just the first).

    Run this once after upgrading to the multi-attachment schema so that
    messages synced under the old one-attachment logic gain their extra photos.
    """
    msg_rows = msg_conn.execute(
        "SELECT rowid FROM messages WHERE has_attachment = 1"
    ).fetchall()
    rowids = [r[0] for r in msg_rows]
    if not rowids:
        return 0

    filled = 0
    for i in range(0, len(rowids), CHUNK):
        chunk = rowids[i:i + CHUNK]
        placeholders = ','.join('?' * len(chunk))
        attach_rows = chat_conn.execute(f"""
            SELECT maj.message_id, a.filename, a.mime_type
            FROM message_attachment_join maj
            JOIN attachment a ON maj.attachment_id = a.ROWID
            WHERE maj.message_id IN ({placeholders})
              AND (a.mime_type LIKE 'image/%' OR a.mime_type LIKE 'video/%')
            ORDER BY maj.message_id ASC, a.ROWID ASC
        """, chunk).fetchall()

        by_msg = {}
        for message_id, filename, mime in attach_rows:
            by_msg.setdefault(message_id, []).append((filename, mime))

        for message_id, atts in by_msg.items():
            copied = []
            for idx, (filename, mime) in enumerate(atts):
                path = copy_attachment(message_id, filename, idx)
                if path:
                    copied.append((idx, path, mime))
                    msg_conn.execute(
                        "INSERT OR IGNORE INTO attachments (message_rowid, idx, path, mime) VALUES (?, ?, ?, ?)",
                        (message_id, idx, path, mime)
                    )
            if copied:
                msg_conn.execute(
                    "UPDATE messages SET attachment_path = ? WHERE rowid = ? AND attachment_path IS NULL",
                    (copied[0][1], message_id)
                )
                filled += 1
                if verbose and len(copied) > 1:
                    print(f"  ROWID={message_id}: {len(copied)} attachments")
        msg_conn.commit()

    return filled


def main():
    parser = argparse.ArgumentParser(description="Sync iMessage beer chat to local messages.db")
    parser.add_argument("--backfill", action="store_true", help="Sync all history from ROWID 0")
    parser.add_argument("--chat-id", help="Sync only this chat ID (default: all known chats)")
    parser.add_argument("--fix-text", action="store_true", help="Re-parse attributedBody for existing NULL-text rows only")
    parser.add_argument("--fix-attachments", action="store_true", help="Copy missing attachment files for existing rows")
    parser.add_argument("--rebuild-attachments", action="store_true", help="Rebuild attachments table with ALL photos/videos per message")
    parser.add_argument("--fix-heic", action="store_true", help="Convert existing .heic attachments to .jpg")
    parser.add_argument("--verbose", action="store_true", help="Print each row as it syncs")
    args = parser.parse_args()

    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)

    msg_conn = sqlite3.connect(MESSAGES_DB)
    init_db(msg_conn)

    chat_conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)

    if args.fix_heic:
        print("[sync] Converting .heic attachments to .jpg...")
        converted = fix_heic(msg_conn, args.verbose)
        print(f"[sync] Converted {converted} files.")
        return

    if args.fix_attachments:
        print("[sync] Backfilling missing attachments...")
        filled = backfill_missing_attachments(msg_conn, chat_conn, args.verbose)
        print(f"[sync] Copied {filled} attachment files.")
        chat_conn.close()
        return

    if args.rebuild_attachments:
        print("[sync] Rebuilding attachments table (all photos/videos per message)...")
        filled = rebuild_attachments(msg_conn, chat_conn, args.verbose)
        print(f"[sync] Populated attachments for {filled} messages.")
        chat_conn.close()
        return

    if args.fix_text:
        print("[sync] Re-parsing attributedBody for existing NULL-text rows...")
        filled_text = backfill_missing_text(msg_conn, chat_conn, args.verbose)
        print(f"[sync] Updated text for {filled_text} rows.")
        chat_conn.close()
        print("[sync] Fixing is_reaction flags...")
        fixed_reactions = fix_reaction_flags(msg_conn, args.verbose)
        print(f"[sync] Fixed {fixed_reactions} reaction flags.")
        return

    chat_ids_to_sync = [args.chat_id] if args.chat_id else CHAT_IDS

    for chat_id in chat_ids_to_sync:
        last_rowid = 0 if args.backfill else get_last_rowid(msg_conn, chat_id)
        print(f"[sync] {'Backfill' if args.backfill else 'Incremental'} sync of {chat_id} from ROWID {last_rowid}...")

        rows = fetch_from_chat_db(chat_conn, last_rowid, chat_id)
        groups = group_by_message(rows)

        if groups:
            count = upsert_messages(msg_conn, groups, chat_id, args.verbose)
            set_last_rowid(msg_conn, chat_id, rows[-1][0])
            print(f"[sync] Inserted {count} rows (last ROWID: {rows[-1][0]})")
        else:
            print(f"[sync] No new messages for {chat_id}.")

    if args.backfill:
        print("[sync] Backfilling text from attributedBody for existing rows...")
        filled_text = backfill_missing_text(msg_conn, chat_conn, args.verbose)
        print(f"[sync] Updated text for {filled_text} rows.")

        print("[sync] Rebuilding attachments table (all photos/videos per message)...")
        filled_attach = rebuild_attachments(msg_conn, chat_conn, args.verbose)
        print(f"[sync] Populated attachments for {filled_attach} messages.")

    chat_conn.close()


if __name__ == "__main__":
    main()
