#!/usr/bin/env python3
"""Turn the drink-log CSV into annotation entries and merge them with a hand pass.

The spreadsheet behind drinks.maximslo.com already holds hand-verified ground truth
(drink number, drinker, type, date) and, in its Photo column, the exact attachment
file each drink was logged with. That filename is generated from the iMessage row id
by copy_attachment() in scripts/sync_messages.py:

    attachments/{message_rowid}.{ext}          first photo of a message
    attachments/{message_rowid}_{idx}.{ext}    2nd+ photo of the same message

so a CSV row maps back to a message deterministically: strip the extension, split on
'_', left part is messages.rowid. Dates never need parsing (the CSV has no year) —
they come from the matched message.

Result: annotation set `--set` = a copy of set `--base-set` (the hand pass, which
always wins) plus one entry per CSV photo-message the hand pass never annotated.

    venv/bin/python3 scripts/import_csv_annotations.py                    # dry run
    venv/bin/python3 scripts/import_csv_annotations.py --apply            # write it
    venv/bin/python3 scripts/import_csv_annotations.py --apply --replace  # rebuild
"""

import argparse
import csv
import os
import shutil
import sqlite3
import sys
from collections import OrderedDict
from datetime import datetime

HOME = os.path.expanduser("~/drinks")
DATA_DIR = os.path.join(HOME, "data")
MESSAGES_DB = os.path.join(DATA_DIR, "messages.db")
ANNOTATIONS_DB = os.path.join(DATA_DIR, "annotations.db")
REPORT_CSV = os.path.join(DATA_DIR, "annotation_import_report.csv")
DEFAULT_CSV = os.path.expanduser(
    "~/Desktop/latest_with_photos - Drink Log - latest_with_photos - Drink Log.csv")


def parse_photo(value):
    """'44800_3.jpeg' -> (44800, 3); '46688.jpeg' -> (46688, 0); junk -> None."""
    name = (value or "").strip()
    if not name or "." not in name:
        return None
    stem = name.rsplit(".", 1)[0]
    head, _, tail = stem.partition("_")
    if not head.isdigit() or (tail and not tail.isdigit()):
        return None
    return int(head), int(tail or 0)


def note_for(rows):
    """'person, n, type' per line — repeats of one (drinker, type) collapse into n."""
    counts = OrderedDict()
    for r in rows:
        key = (r["Drinker"].strip().lower(), r["Type"].strip().lower())
        counts[key] = counts.get(key, 0) + 1
    return "\n".join(f"{person}, {n}, {drink}" for (person, drink), n in counts.items())


def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def collect(rows, messages):
    """Group CSV rows by message rowid. Returns (entries, skipped).

    Keyed by message, not by filename: the DB allows one group per message per set,
    and some messages carry several photos that the CSV lists as separate rows.
    """
    entries, skipped = OrderedDict(), []
    for r in rows:
        if not (r.get("Photo") or "").strip():
            skipped.append((r, "no_photo"))
            continue
        parsed = parse_photo(r["Photo"])
        if not parsed or parsed[0] not in messages:
            skipped.append((r, "unresolved"))
            continue
        entries.setdefault(parsed[0], []).append(r)
    return entries, skipped


def write_report(skipped, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["#", "Drinker", "Type", "Info", "Date", "Photo", "reason"])
        for r, reason in skipped:
            w.writerow([r.get("#", ""), r.get("Drinker", ""), r.get("Type", ""),
                        r.get("Info", ""), r.get("Date", ""), r.get("Photo", ""), reason])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--set", type=int, default=3, dest="set_id", help="destination set")
    ap.add_argument("--base-set", type=int, default=2, help="hand pass to copy in first; 0 = none")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--replace", action="store_true", help="wipe the destination set first")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        sys.exit(f"CSV not found: {args.csv}")

    msg = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
    messages = {r[0]: r[1] for r in msg.execute("SELECT rowid, chat_id FROM messages")}
    msg.close()

    rows = load_csv(args.csv)
    entries, skipped = collect(rows, messages)

    ann = sqlite3.connect(ANNOTATIONS_DB)
    ann.row_factory = sqlite3.Row
    ann.execute("PRAGMA foreign_keys = ON")
    taken = {r[0] for r in ann.execute(
        "SELECT message_rowid FROM group_messages WHERE set_id = ?", (args.base_set,))}
    existing = ann.execute("SELECT COUNT(*) FROM groups WHERE set_id = ?", (args.set_id,)).fetchone()[0]

    new = OrderedDict((rowid, rs) for rowid, rs in entries.items() if rowid not in taken)
    overlap_rows = sum(len(rs) for rowid, rs in entries.items() if rowid in taken)
    base_groups = ann.execute("SELECT COUNT(*) FROM groups WHERE set_id = ?", (args.base_set,)).fetchone()[0]
    for rowid, rs in entries.items():
        if rowid in taken:
            skipped += [(r, f"already_in_set{args.base_set}") for r in rs]

    print(f"CSV rows                      {len(rows):>6}")
    print(f"  no photo          skipped   {sum(1 for _, s in skipped if s == 'no_photo'):>6}")
    print(f"  unresolvable      skipped   {sum(1 for _, s in skipped if s == 'unresolved'):>6}")
    print(f"photo messages                {len(entries):>6}")
    print(f"  in set {args.base_set}          skipped   {len(entries) - len(new):>6}  ({overlap_rows} csv rows)")
    print(f"  imported as entries         {len(new):>6}  ({sum(len(v) for v in new.values())} csv rows)")
    print(f"set {args.set_id} total = {base_groups} + {len(new)} = {base_groups + len(new)}")

    write_report(skipped, REPORT_CSV)
    print(f"[report] {len(skipped)} skipped rows -> {REPORT_CSV}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        for rowid in list(new)[:2]:
            print(f"\n  message {rowid}:\n" + "\n".join("    " + l for l in note_for(new[rowid]).split("\n")))
        ann.close()
        return

    if existing and not args.replace:
        ann.close()
        sys.exit(f"set {args.set_id} already has {existing} groups — pass --replace to rebuild it")

    backup = f"{ANNOTATIONS_DB}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(ANNOTATIONS_DB, backup)
    print(f"[backup] {backup}")

    with ann:  # one transaction: nothing lands unless it all does
        if existing:
            ann.execute("DELETE FROM groups WHERE set_id = ?", (args.set_id,))
            ann.execute("DELETE FROM group_messages WHERE set_id = ?", (args.set_id,))

        copied = 0
        if args.base_set:
            for g in ann.execute("SELECT * FROM groups WHERE set_id = ? ORDER BY id",
                                 (args.base_set,)).fetchall():
                cur = ann.execute(
                    "INSERT INTO groups (set_id, chat_id, note, solo, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (args.set_id, g["chat_id"], g["note"], g["solo"],
                     g["created_at"], g["updated_at"]))
                ann.execute(
                    "INSERT INTO group_messages (set_id, message_rowid, group_id) "
                    "SELECT ?, message_rowid, ? FROM group_messages WHERE group_id = ?",
                    (args.set_id, cur.lastrowid, g["id"]))
                copied += 1

        for rowid, rs in new.items():
            cur = ann.execute(
                "INSERT INTO groups (set_id, chat_id, note, solo) VALUES (?, ?, ?, 0)",
                (args.set_id, messages.get(rowid), note_for(rs)))
            ann.execute(
                "INSERT INTO group_messages (set_id, message_rowid, group_id) VALUES (?, ?, ?)",
                (args.set_id, rowid, cur.lastrowid))

    total = ann.execute("SELECT COUNT(*) FROM groups WHERE set_id = ?", (args.set_id,)).fetchone()[0]
    print(f"[done] set {args.set_id}: {copied} copied from set {args.base_set} + {len(new)} imported = {total}")
    ann.close()


if __name__ == "__main__":
    main()
