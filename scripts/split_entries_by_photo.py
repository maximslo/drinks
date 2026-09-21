#!/usr/bin/env python3
"""Give every photo its own annotation entry.

An entry used to be attached to a whole message, so a message carrying several
photos showed the same annotation on each of them in the validation grid — and
editing one edited all of them. This splits those entries: the original keeps the
message's first photo, and each extra photo gets its own entry with a copy of the
note and the Solo flag. Nothing is deleted and no note text changes.

    venv/bin/python3 scripts/split_entries_by_photo.py --set 3           # dry run
    venv/bin/python3 scripts/split_entries_by_photo.py --set 3 --apply
"""

import argparse
import os
import shutil
import sqlite3
from datetime import datetime

DATA_DIR = os.path.expanduser("~/drinks/data")
MESSAGES_DB = os.path.join(DATA_DIR, "messages.db")
ANNOTATIONS_DB = os.path.join(DATA_DIR, "annotations.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", type=int, default=3, dest="set_id")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(ANNOTATIONS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("ATTACH DATABASE ? AS msg", (MESSAGES_DB,))

    # every attachment of every message in this set, with the entry holding it
    rows = conn.execute("""
        SELECT gm.group_id, gm.message_rowid, a.idx
        FROM group_messages gm
        JOIN msg.attachments a ON a.message_rowid = gm.message_rowid
        WHERE gm.set_id = ? AND gm.idx = 0
        ORDER BY gm.group_id, gm.message_rowid, a.idx
    """, (args.set_id,)).fetchall()

    extra = {}          # group_id -> [(rowid, idx), ...] photos beyond the first
    for r in rows:
        if r["idx"] != 0:
            extra.setdefault(r["group_id"], []).append((r["message_rowid"], r["idx"]))

    total_new = sum(len(v) for v in extra.values())
    print(f"set {args.set_id}: {len(extra)} entries cover extra photos -> {total_new} new entries")
    if not args.apply:
        for gid, photos in list(extra.items())[:3]:
            note = conn.execute("SELECT note FROM groups WHERE id = ?", (gid,)).fetchone()["note"]
            print(f"  entry {gid} {photos} :: {note.splitlines()[:1]}")
        print("\nDry run. Re-run with --apply to write.")
        return

    backup = f"{ANNOTATIONS_DB}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(ANNOTATIONS_DB, backup)
    print(f"[backup] {backup}")

    made = 0
    with conn:
        for gid, photos in extra.items():
            g = conn.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
            for rowid, idx in photos:
                cur = conn.execute(
                    "INSERT INTO groups (set_id, chat_id, note, solo, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (g["set_id"], g["chat_id"], g["note"], g["solo"],
                     g["created_at"], g["updated_at"]))
                conn.execute(
                    "INSERT OR REPLACE INTO group_messages (set_id, message_rowid, idx, group_id) "
                    "VALUES (?, ?, ?, ?)", (g["set_id"], rowid, idx, cur.lastrowid))
                made += 1

    total = conn.execute("SELECT COUNT(*) FROM groups WHERE set_id = ?", (args.set_id,)).fetchone()[0]
    print(f"[done] created {made} entries; set {args.set_id} now has {total}")
    conn.close()


if __name__ == "__main__":
    main()
