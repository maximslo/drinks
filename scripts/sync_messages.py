import sqlite3
import argparse
import os
import shutil
from datetime import datetime, timezone

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
MESSAGES_DB = os.path.expanduser("~/drinks/data/messages.db")
ATTACHMENTS_DIR = os.path.expanduser("~/drinks/data/attachments")
CHAT_ID = "chat313739884378608609"

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
            attachment_path TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN attachment_path TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def get_last_rowid(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='last_synced_rowid'").fetchone()
    return int(row[0]) if row else 0


def set_last_rowid(conn, rowid):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_synced_rowid', ?)", (rowid,))
    conn.commit()


def fetch_from_chat_db(chat_conn, last_rowid):
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
            AND a.mime_type LIKE 'image/%'
        WHERE c.chat_identifier = ?
          AND m.ROWID > ?
        ORDER BY m.ROWID ASC, a.ROWID ASC
    """, (CHAT_ID, last_rowid)).fetchall()


def deduplicate_rows(rows):
    """Keep first image attachment per message ROWID."""
    seen = {}
    for row in rows:
        rowid = row[0]
        if rowid not in seen:
            seen[rowid] = row
        elif row[7] and not seen[rowid][7]:  # row[7] = attach_filename
            seen[rowid] = row
    return list(seen.values())


def copy_attachment(rowid, src_path):
    if not src_path:
        return None
    src = os.path.expanduser(src_path)
    if not os.path.exists(src):
        return None
    ext = src_path.rsplit('.', 1)[-1].lower() if '.' in src_path else 'jpg'
    dst = os.path.join(ATTACHMENTS_DIR, f"{rowid}.{ext}")
    if os.path.exists(dst):
        return f"attachments/{rowid}.{ext}"
    shutil.copy2(src, dst)
    return f"attachments/{rowid}.{ext}"


def upsert_messages(msg_conn, rows, verbose):
    count = 0
    for rowid, phone, raw_text, attributed_body, date_val, is_from_me, has_attachment, attach_filename, attach_mime in rows:
        sent_at = apple_date_to_iso(date_val)
        text = resolve_text(raw_text, attributed_body)
        reaction = 1 if is_reaction(text) else 0
        attach_path = copy_attachment(rowid, attach_filename)
        msg_conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(rowid, phone, text, sent_at, is_from_me, has_attachment, is_reaction, attachment_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rowid, phone, text, sent_at, is_from_me or 0, has_attachment or 0, reaction, attach_path)
        )
        count += 1
        if verbose:
            print(f"  ROWID={rowid} phone={phone} sent_at={sent_at} reaction={reaction} attach={attach_path} text={str(text)[:50]!r}")
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
              AND a.mime_type LIKE 'image/%'
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


def main():
    parser = argparse.ArgumentParser(description="Sync iMessage beer chat to local messages.db")
    parser.add_argument("--backfill", action="store_true", help="Sync all history from ROWID 0")
    parser.add_argument("--fix-text", action="store_true", help="Re-parse attributedBody for existing NULL-text rows only")
    parser.add_argument("--verbose", action="store_true", help="Print each row as it syncs")
    args = parser.parse_args()

    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)

    msg_conn = sqlite3.connect(MESSAGES_DB)
    init_db(msg_conn)

    chat_conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)

    if args.fix_text:
        print("[sync] Re-parsing attributedBody for existing NULL-text rows...")
        filled_text = backfill_missing_text(msg_conn, chat_conn, args.verbose)
        print(f"[sync] Updated text for {filled_text} rows.")
        chat_conn.close()
        print("[sync] Fixing is_reaction flags...")
        fixed_reactions = fix_reaction_flags(msg_conn, args.verbose)
        print(f"[sync] Fixed {fixed_reactions} reaction flags.")
        return

    last_rowid = 0 if args.backfill else get_last_rowid(msg_conn)
    print(f"[sync] {'Backfill' if args.backfill else 'Incremental'} sync from ROWID {last_rowid}...")

    rows = fetch_from_chat_db(chat_conn, last_rowid)
    rows = deduplicate_rows(rows)

    if rows:
        count = upsert_messages(msg_conn, rows, args.verbose)
        last_new_rowid = rows[-1][0]
        set_last_rowid(msg_conn, last_new_rowid)
        print(f"[sync] Inserted {count} rows (last ROWID: {last_new_rowid})")
    else:
        print("[sync] No new messages.")

    if args.backfill:
        print("[sync] Backfilling text from attributedBody for existing rows...")
        filled_text = backfill_missing_text(msg_conn, chat_conn, args.verbose)
        print(f"[sync] Updated text for {filled_text} rows.")

        print("[sync] Backfilling attachments for existing rows...")
        filled_attach = backfill_missing_attachments(msg_conn, chat_conn, args.verbose)
        print(f"[sync] Backfilled {filled_attach} attachments.")

    chat_conn.close()


if __name__ == "__main__":
    main()
