#!/usr/bin/env python3
"""
Deterministic photo <-> drink-row matcher.

Builds data/photo_match.db: a chronological queue of every photo/video attachment,
each pre-assigned a "span" = how many consecutive drink rows (from the source-of-truth
CSV) it covers. This is the best-effort automated pass; a human then walks the queue in
the review UI and corrects the tough calls.

Why order, not numbers: the typed drink numbers in the chat drift by hundreds over the
year, so they are unreliable as IDs. Matching is driven by chronological order + the
CSV `Date` column (the strongest independent signal), with numbers kept only for context.

Tables in photo_match.db:
  photo_queue   - one row per attachment, the reviewable queue (see SCHEMA below)
  drinks        - snapshot of the CSV in chronological order (order_index 0 = first drink)
  meta          - csv_path, tz, built_at, totals

Reusable by the API:
  build_db(...)   - (re)build everything from the CSV + messages.db
  recompute(conn) - re-derive start_drink_id for every queue row from current spans
"""

import os
import sys
import csv
import json
import re
import sqlite3
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo always present on 3.9+ here
    LOCAL_TZ = timezone.utc

# Reuse the chat number parser for context/tiebreaks.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "watcher"))
from parser import parse_numbers  # noqa: E402

HOME = os.path.expanduser("~/drinks")
MESSAGES_DB = os.path.join(HOME, "data", "messages.db")
MATCH_DB = os.path.join(HOME, "data", "photo_match.db")
DEFAULT_CSV = os.path.expanduser("~/Desktop/latest.csv")

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


# ─── CSV (source of truth) ──────────────────────────────────────────────────────

def load_csv_drinks(csv_path):
    """Return drinks in chronological order (oldest first) with reconstructed full dates.

    The CSV has no year; it spans May 2025 -> June 2026 with exactly one Dec->Jan wrap.
    We walk the rows in chronological order (descending ID) and bump the year on the wrap.
    """
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    drinks = []
    for r in rows[1:]:
        if len(r) < 8:
            r = r + [""] * (8 - len(r))
        did = r[0].strip()
        if not did or did == "---" or not did.lstrip("-").isdigit():
            continue
        if not r[1].strip():          # no Drinker -> reserved/empty row, skip
            continue
        drinks.append({
            "drink_id": int(did),
            "drinker": r[1].strip(),
            "type": r[2].strip(),
            "info": r[3].strip(),
            "date_str": r[4].strip(),
            "people": r[5].strip(),
            "event": r[6].strip(),
            "location": r[7].strip(),
        })
    drinks.sort(key=lambda d: -d["drink_id"])  # chronological (oldest/highest id first)

    year, prev_month = 2025, None
    for d in drinks:
        m = re.match(r"([A-Za-z]+)\s+(\d+)", d["date_str"])
        if m and m.group(1).capitalize() in MONTHS:
            mon, day = MONTHS[m.group(1).capitalize()], int(m.group(2))
            if prev_month is not None and mon < prev_month - 3:  # Dec -> Jan wrap
                year += 1
            prev_month = mon
            d["date_key"] = f"{year:04d}-{mon:02d}-{day:02d}"
        else:
            d["date_key"] = None
    return header, drinks


# ─── Photos (chronological queue) ───────────────────────────────────────────────

def load_photos(messages_db):
    """Every attachment ordered by send time, with local date + parsed numbers."""
    conn = sqlite3.connect(messages_db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT a.message_rowid AS rowid, a.idx AS idx, a.path AS path, a.mime AS mime,
               m.sent_at AS sent_at, m.text AS text, m.phone AS phone, m.chat_id AS chat_id
        FROM attachments a JOIN messages m ON m.rowid = a.message_rowid
        ORDER BY m.sent_at ASC, a.message_rowid ASC, a.idx ASC
    """).fetchall()
    conn.close()
    photos = []
    for r in rows:
        dt = datetime.strptime(r["sent_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        local = dt.astimezone(LOCAL_TZ)
        text = (r["text"] or "").lstrip("￼").strip()
        nums = [n for (n, _d, _s) in parse_numbers(text)] if text else []
        photos.append({
            "attachment_rowid": r["rowid"],
            "attachment_idx": r["idx"],
            "attachment_path": r["path"],
            "mime": r["mime"],
            "sent_at": r["sent_at"],
            "local_date": local.strftime("%Y-%m-%d"),
            "msg_text": text,
            "numbers": nums,
            "chat_id": r["chat_id"],
        })
    return photos


# ─── Span assignment (date-bucketed distribution) ───────────────────────────────

def assign_spans(drinks, photos):
    """Compute suggested_span + skip_before for each photo.

    For each calendar date: distribute that date's drink rows evenly across that date's
    photos (remainder to the earliest photos). Dates with photos but no drinks -> span 0
    (non-drink). Dates with drinks but no photos -> those rows become `skip_before` on the
    next photo (rows that will be left blank). Confidence reflects how clean the bucket is.
    """
    from collections import defaultdict, OrderedDict

    photos_by_date = defaultdict(list)
    for i, p in enumerate(photos):
        photos_by_date[p["local_date"]].append(i)

    drinks_by_date = OrderedDict()
    for idx, d in enumerate(drinks):
        drinks_by_date.setdefault(d["date_key"], []).append(idx)

    span = [0] * len(photos)
    skip_before = [0] * len(photos)
    confidence = [0.4] * len(photos)

    all_dates = sorted(set(photos_by_date) | {k for k in drinks_by_date if k},
                       key=lambda s: s)
    pending_skip = 0
    for date in all_dates:
        rws = drinks_by_date.get(date, [])
        phs = photos_by_date.get(date, [])
        if phs and rws:
            base, rem = divmod(len(rws), len(phs))
            clean = (len(rws) == len(phs))
            for k, pi in enumerate(phs):
                span[pi] = base + (1 if k < rem else 0)
                confidence[pi] = 0.9 if clean else 0.5
            phs2 = list(phs)
            skip_before[phs2[0]] += pending_skip
            pending_skip = 0
        elif phs and not rws:
            for pi in phs:
                span[pi] = 0
                confidence[pi] = 0.4          # likely non-drink, but unverified
            skip_before[phs[0]] += pending_skip
            pending_skip = 0
        elif rws and not phs:
            pending_skip += len(rws)           # orphan rows -> blank, carried forward
    return span, skip_before, confidence


def _walk_assignment(queue, drink_ids, deferred):
    """Yield (order_index, covered_ids, pointer_id) for every photo.

    Two ways a photo gets drinks:
      • PINNED (pinned_ids set): the photo is assigned to that exact SET of drinks, OUT of the
        flow — they are pulled from the pool so the sequential walk never touches them. Used
        for out-of-order / multi-row cases (e.g. parallel drinkers, a group photo a reviewer
        hand-picks). An explicit empty list means "covers nothing" (non-drink), still out of flow.
      • FLOW (pinned_ids NULL): the photo covers the next `span` drinks from the walk pointer,
        over the pool of drinks that are neither pinned nor deferred. `skip_before` skips first.

    `deferred` drinks are held out of the flow ("at the top of the queue") until assigned.
    pointer_id is the pool position even when a photo covers nothing (for UI context).
    """
    idset = set(drink_ids)
    pins_by, pinned_all = {}, set()
    for q in queue:
        pj = q["pinned_ids"]
        if pj is not None:
            ids = {d for d in json.loads(pj) if d in idset and d not in deferred}
            pins_by[q["order_index"]] = ids
            pinned_all |= ids
    pool = [d for d in drink_ids if d not in pinned_all and d not in deferred]

    pi = 0
    for q in queue:
        oi = q["order_index"]
        if oi in pins_by:
            covered = [d for d in drink_ids if d in pins_by[oi]]   # in chronological order
            yield oi, covered, (covered[0] if covered else None)
            continue
        eff = q["assigned_span"] if q["assigned_span"] is not None else q["suggested_span"]
        pi += q["skip_before"] or 0
        pointer = pool[pi] if pi < len(pool) else None
        covered = pool[pi:pi + eff] if eff else []
        pi += eff
        yield oi, covered, pointer


# ─── Build / recompute ──────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE photo_queue (
    order_index      INTEGER PRIMARY KEY,
    attachment_rowid INTEGER NOT NULL,
    attachment_idx   INTEGER NOT NULL,
    attachment_path  TEXT,
    attachment_file  TEXT,           -- basename, written into the CSV Photo column
    mime             TEXT,
    sent_at          TEXT,
    local_date       TEXT,
    msg_text         TEXT,
    parsed_numbers   TEXT,           -- JSON list
    suggested_span   INTEGER NOT NULL DEFAULT 0,
    assigned_span    INTEGER,        -- human override; NULL = unreviewed
    skip_before      INTEGER NOT NULL DEFAULT 0,
    start_drink_id   INTEGER,        -- derived (NULL = covers no row)
    status           TEXT NOT NULL DEFAULT 'auto',  -- 'auto' | 'confirmed' | 'non_drink'
    confidence       REAL,
    notes            TEXT,
    pinned_ids       TEXT,        -- JSON list of drinks pinned to this photo (out of flow);
                                  --   NULL = use the flow span instead
    pointer_drink_id INTEGER,     -- where the sequential walk pointer sits (context)
    covered_ids      TEXT         -- JSON list of drink_ids this photo covers (materialized)
);
CREATE TABLE drinks (
    order_index INTEGER PRIMARY KEY,  -- 0 = oldest (highest drink_id)
    drink_id    INTEGER UNIQUE,
    drinker TEXT, type TEXT, info TEXT, date_str TEXT, date_key TEXT,
    people TEXT, event TEXT, location TEXT,
    deferred INTEGER NOT NULL DEFAULT 0  -- held out of flow ("at top of queue") until assigned
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX idx_queue_start ON photo_queue(start_drink_id);
"""


def recompute(conn):
    """Re-derive covered_ids + start_drink_id + pointer_drink_id for every queue row."""
    conn.row_factory = sqlite3.Row
    drink_ids = [r[0] for r in conn.execute(
        "SELECT drink_id FROM drinks ORDER BY order_index")]
    deferred = {r[0] for r in conn.execute(
        "SELECT drink_id FROM drinks WHERE deferred = 1")}
    queue = [dict(r) for r in conn.execute(
        "SELECT order_index, suggested_span, assigned_span, skip_before, pinned_ids "
        "FROM photo_queue ORDER BY order_index")]
    cur = conn.cursor()
    for oi, covered, pointer in _walk_assignment(queue, drink_ids, deferred):
        cur.execute(
            "UPDATE photo_queue SET covered_ids=?, start_drink_id=?, pointer_drink_id=? "
            "WHERE order_index=?",
            (json.dumps(covered), covered[0] if covered else None, pointer, oi))
    conn.commit()


def build_db(csv_path=DEFAULT_CSV, messages_db=MESSAGES_DB, match_db=MATCH_DB):
    header, drinks = load_csv_drinks(csv_path)
    photos = load_photos(messages_db)
    span, skip_before, confidence = assign_spans(drinks, photos)

    if os.path.exists(match_db):
        os.remove(match_db)
    conn = sqlite3.connect(match_db)
    conn.executescript(SCHEMA)
    cur = conn.cursor()

    for oi, d in enumerate(drinks):
        cur.execute(
            "INSERT INTO drinks (order_index, drink_id, drinker, type, info, date_str, "
            "date_key, people, event, location) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (oi, d["drink_id"], d["drinker"], d["type"], d["info"], d["date_str"],
             d["date_key"], d["people"], d["event"], d["location"]))

    for oi, p in enumerate(photos):
        cur.execute(
            "INSERT INTO photo_queue (order_index, attachment_rowid, attachment_idx, "
            "attachment_path, attachment_file, mime, sent_at, local_date, msg_text, "
            "parsed_numbers, suggested_span, assigned_span, skip_before, status, confidence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oi, p["attachment_rowid"], p["attachment_idx"], p["attachment_path"],
             os.path.basename(p["attachment_path"]) if p["attachment_path"] else None,
             p["mime"], p["sent_at"], p["local_date"], p["msg_text"],
             json.dumps(p["numbers"]), span[oi], None, skip_before[oi],
             "auto", confidence[oi]))

    for k, v in {
        "csv_path": csv_path, "tz": str(LOCAL_TZ), "header": json.dumps(header),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "total_drinks": str(len(drinks)), "total_photos": str(len(photos)),
    }.items():
        cur.execute("INSERT INTO meta (key, value) VALUES (?,?)", (k, v))
    conn.commit()
    recompute(conn)
    return conn, drinks, photos


# ─── Export ─────────────────────────────────────────────────────────────────────

def build_photo_map(conn):
    """Return {drink_id: attachment_file} from the current queue assignment."""
    conn.row_factory = sqlite3.Row
    queue = [dict(r) for r in conn.execute(
        "SELECT covered_ids, attachment_file FROM photo_queue ORDER BY order_index")]
    photo_map = {}
    for it in queue:
        for did in json.loads(it["covered_ids"] or "[]"):
            photo_map[did] = it["attachment_file"]   # later photo wins on any overlap
    return photo_map


def _export_rows(match_db):
    """Build output rows from the DB snapshot only (never reads the source CSV file, so it
    works from a sandboxed daemon that can't reach the Desktop). Columns mirror the source:
    ---, Drinker, Type, Info, Date, People In Photo, Event, Location, + Photo."""
    conn = sqlite3.connect(match_db)
    conn.row_factory = sqlite3.Row
    hrow = conn.execute("SELECT value FROM meta WHERE key='header'").fetchone()
    header = json.loads(hrow[0]) if hrow else \
        ["\\---", "Drinker", "Type", "Info", "Date", "People In Photo", "Event", "Location"]
    photo_map = build_photo_map(conn)
    drinks = conn.execute(
        "SELECT drink_id, drinker, type, info, date_str, people, event, location "
        "FROM drinks ORDER BY order_index").fetchall()
    conn.close()
    out = [list(header) + ["Photo"]]
    filled = 0
    for d in drinks:
        photo = photo_map.get(d["drink_id"], "")
        if photo:
            filled += 1
        out.append([d["drink_id"], d["drinker"], d["type"], d["info"], d["date_str"],
                    d["people"], d["event"], d["location"], photo])
    return out, filled, len(photo_map)


def export_csv_text(match_db=MATCH_DB):
    """Return the augmented CSV as a string (for streaming a browser download)."""
    import io
    out, filled, mapped = _export_rows(match_db)
    buf = io.StringIO()
    csv.writer(buf).writerows(out)
    return buf.getvalue(), filled, mapped


def export_csv(match_db=MATCH_DB, csv_out=None):
    """Write the augmented CSV to disk (CLI use; a terminal has Desktop access)."""
    if csv_out is None:
        conn = sqlite3.connect(match_db)
        row = conn.execute("SELECT value FROM meta WHERE key='csv_path'").fetchone()
        conn.close()
        csv_in = row[0] if row else os.path.expanduser("~/Desktop/latest.csv")
        base, ext = os.path.splitext(csv_in)
        csv_out = f"{base}_with_photos{ext}"
    out, filled, mapped = _export_rows(match_db)
    with open(csv_out, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(out)
    return csv_out, filled, mapped


# ─── CLI ────────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if args and args[0] == "--export":
        out, filled, mapped = export_csv()
        print(f"Exported {out}\n  rows with a photo: {filled}   distinct drinks mapped: {mapped}")
        return
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    # Guard: a rebuild wipes human review progress. Refuse if reviews exist.
    if os.path.exists(MATCH_DB) and not force:
        try:
            c = sqlite3.connect(MATCH_DB)
            n = c.execute("SELECT COUNT(*) FROM photo_queue "
                          "WHERE status IN ('confirmed','non_drink')").fetchone()[0]
            c.close()
        except Exception:
            n = 0
        if n:
            print(f"Refusing to rebuild: {n} reviewed items in {MATCH_DB} would be lost.\n"
                  f"Re-run with --force to discard them.")
            return
    csv_path = args[0] if args else DEFAULT_CSV
    conn, drinks, photos = build_db(csv_path=csv_path)
    total_drinks = len(drinks)
    spans = conn.execute("SELECT COALESCE(assigned_span, suggested_span) FROM photo_queue").fetchall()
    span_sum = sum(s[0] for s in spans)
    covered = conn.execute(
        "SELECT COUNT(DISTINCT order_index) FROM photo_queue "
        "WHERE start_drink_id IS NOT NULL").fetchone()[0]
    nondrink = conn.execute(
        "SELECT COUNT(*) FROM photo_queue WHERE COALESCE(assigned_span, suggested_span)=0").fetchone()[0]
    print(f"Built {MATCH_DB}")
    print(f"  drinks: {total_drinks}   photos: {len(photos)}")
    print(f"  sum(span): {span_sum}   (should be <= {total_drinks}; orphan rows stay blank)")
    print(f"  photos covering >=1 row: {covered}   non-drink (span 0): {nondrink}")
    print("  first 6 queue items:")
    for r in conn.execute(
        "SELECT order_index, sent_at, local_date, start_drink_id, "
        "COALESCE(assigned_span,suggested_span) sp, substr(msg_text,1,20) t "
        "FROM photo_queue ORDER BY order_index LIMIT 6"):
        print(f"    #{r['order_index']} {r['sent_at']} -> drink {r['start_drink_id']} "
              f"span {r['sp']}  {r['t']!r}")
    conn.close()


if __name__ == "__main__":
    main()
