#!/usr/bin/env python3
"""Pre-generate the feed thumbnails served by /thumb/{rowid}/{idx}.

The API builds these on demand, but a cold cache means the messages UI shows grey
boxes while it scrolls. This fills data/thumbs/ up front, in parallel.

    venv/bin/python3 scripts/build_thumbs.py [--force] [--jobs N]
"""

import argparse
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor

HOME = os.path.expanduser("~/drinks")
DATA_DIR = os.path.join(HOME, "data")
MESSAGES_DB = os.path.join(DATA_DIR, "messages.db")
THUMBS_DIR = os.path.join(DATA_DIR, "thumbs")
THUMB_MAX = (640, 640)


def targets():
    """(rowid, idx, source path) for every image the feed can show."""
    conn = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT message_rowid AS rowid, idx, path FROM attachments WHERE path IS NOT NULL
        UNION
        SELECT rowid, 0, attachment_path FROM messages
        WHERE has_attachment = 1 AND attachment_path IS NOT NULL
          AND rowid NOT IN (SELECT message_rowid FROM attachments WHERE idx = 0)
    """).fetchall()
    conn.close()
    return [(r["rowid"], r["idx"], r["path"]) for r in rows]


def build(job):
    rowid, idx, rel, force = job
    src = os.path.join(DATA_DIR, rel)
    if not os.path.exists(src):
        return "missing"
    if rel.lower().endswith((".mov", ".mp4", ".m4v")):
        return "video"
    dst = os.path.join(THUMBS_DIR, f"{rowid}_{idx}.jpg")
    if not force and os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        return "cached"
    try:
        from PIL import Image, ImageOps
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail(THUMB_MAX)
            im.convert("RGB").save(dst + ".tmp", "JPEG", quality=78, optimize=True)
        os.replace(dst + ".tmp", dst)
        return "built"
    except Exception:
        return "failed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rebuild even if cached")
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    args = ap.parse_args()

    os.makedirs(THUMBS_DIR, exist_ok=True)
    jobs = [(r, i, p, args.force) for r, i, p in targets()]
    print(f"[thumbs] {len(jobs)} attachments, {args.jobs} workers...")

    tally = {}
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        for n, result in enumerate(pool.map(build, jobs, chunksize=16), 1):
            tally[result] = tally.get(result, 0) + 1
            if n % 500 == 0:
                print(f"  {n}/{len(jobs)} {tally}", flush=True)
    print(f"[thumbs] done: {tally}")


if __name__ == "__main__":
    main()
