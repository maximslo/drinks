import sqlite3
import os
import sys
import json
import mimetypes
import asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional, List
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

DRINKS_DB = os.path.expanduser("~/drinks/data/drinks.db")
MESSAGES_DB = os.path.expanduser("~/drinks/data/messages.db")
MATCH_DB = os.path.expanduser("~/drinks/data/photo_match.db")
DATA_DIR = os.path.expanduser("~/drinks/data")

# Reuse the matcher's recompute/export so the queue stays consistent after edits.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import match_photos  # noqa: E402

BASELINE = {
    "Hunter": 992,
    "Joseph": 866,
    "Jacob":  798,
    "Lucas":  587,
    "Miggy":  375,
    "Marek":  267,
    "Maxim":  226,
    "Cole":   218,
    "Avi":    194,
    "Liam":   263,
    "Owen":   119,
    "Kacper":  95,
}

PHONE_TO_NAME = {
    "+17147429858": "Hunter",   "+16037930991": "Lucas",
    "+18453002491": "Liam",     "+16173097007": "Joseph",
    "+19177562941": "Kacper",   "+16176315336": "Miggy",
    "+14083321330": "Marek",    "+19499759060": "Owen",
    "+17812050278": "Maxim",    "+16179130745": "Jacob",
    "+19497011751": "Avi",      "+19842608337": "Cole",
    "josephteruel@icloud.com": "Joseph",
    "marek.pinto@icloud.com": "Marek",
    "jakestein120@icloud.com": "Jacob",
}

NAME_TO_PHONES = {}
for phone, name in PHONE_TO_NAME.items():
    NAME_TO_PHONES.setdefault(name, []).append(phone)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "../frontend")
app.mount("/ui", StaticFiles(directory=FRONTEND_DIR), name="frontend")

subscribers: list = []


# ─── DB helpers ───────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DRINKS_DB)
    conn.row_factory = sqlite3.Row
    return conn

def get_messages_db():
    conn = sqlite3.connect(MESSAGES_DB)
    conn.row_factory = sqlite3.Row
    return conn

def get_leaderboard_data():
    conn = get_db()
    rows = conn.execute("""
        SELECT person, COUNT(*) as total, MAX(drink_number) as latest_drink_number
        FROM drinks
        GROUP BY person
    """).fetchall()
    conn.close()
    logged = {r["person"]: dict(r) for r in rows}
    result = []
    for person, baseline in BASELINE.items():
        logged_total = logged.get(person, {}).get("total", 0)
        latest = logged.get(person, {}).get("latest_drink_number")
        result.append({
            "person": person,
            "total": baseline + logged_total,
            "baseline": baseline,
            "logged": logged_total,
            "latest_drink_number": latest,
        })
    return sorted(result, key=lambda r: r["total"], reverse=True)


# ─── SSE leaderboard ──────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    asyncio.create_task(watch_drinks_db())

async def watch_drinks_db():
    last_snapshot = None
    while True:
        await asyncio.sleep(1)
        try:
            current = get_leaderboard_data()
            if current != last_snapshot:
                last_snapshot = current
                for q in subscribers:
                    await q.put(current)
        except Exception:
            pass

@app.get("/leaderboard/stream")
async def leaderboard_stream(request: Request):
    async def event_gen():
        q: asyncio.Queue = asyncio.Queue()
        subscribers.append(q)
        try:
            yield {"data": json.dumps(get_leaderboard_data())}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=30)
                    yield {"data": json.dumps(data)}
                except asyncio.TimeoutError:
                    yield {"comment": "keepalive"}
        finally:
            if q in subscribers:
                subscribers.remove(q)
    return EventSourceResponse(event_gen())


# ─── REST endpoints ───────────────────────────────────────────────────────────

@app.get("/leaderboard")
def leaderboard():
    return get_leaderboard_data()

@app.get("/drinks")
def drinks(limit: int = 50, offset: int = 0, person: str = None):
    conn = get_db()
    if person:
        rows = conn.execute("""
            SELECT * FROM drinks
            WHERE person = ?
            ORDER BY drink_number DESC
            LIMIT ? OFFSET ?
        """, (person, limit, offset)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM drinks
            ORDER BY drink_number DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/total")
def total():
    conn = get_db()
    row = conn.execute("SELECT COUNT(*) as count, MAX(drink_number) as latest FROM drinks").fetchone()
    conn.close()
    return {
        "total_logged": row["count"],
        "latest_drink": row["latest"],
        "goal": 10000,
        "remaining": 10000 - (row["latest"] or 0)
    }

@app.get("/recent")
def recent(limit: int = 10):
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM drinks
        ORDER BY imessage_id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/chats")
def get_chats():
    conn = get_messages_db()
    rows = conn.execute(
        "SELECT chat_id, COUNT(*) as message_count FROM messages WHERE chat_id IS NOT NULL GROUP BY chat_id ORDER BY message_count DESC"
    ).fetchall()
    conn.close()
    labels = {
        "chat313739884378608609": "Main",
        "chat247636595391927399": "OG",
        "chat26176758262309627":  "OG (oldest)",
    }
    return [{"chat_id": r["chat_id"], "label": labels.get(r["chat_id"], r["chat_id"]), "message_count": r["message_count"]} for r in rows]


@app.get("/messages")
def get_messages(
    limit: int = 100,
    offset: int = 0,
    name: str = None,
    show_reactions: bool = False,
    search: str = None,
    chat_id: str = None,
):
    conn = get_messages_db()
    conditions = []
    params = []

    conditions.append("(text IS NOT NULL OR has_attachment = 1)")

    if not show_reactions:
        conditions.append("is_reaction = 0")

    if name:
        phones = NAME_TO_PHONES.get(name, [])
        if phones:
            placeholders = ",".join("?" * len(phones))
            conditions.append(f"phone IN ({placeholders})")
            params.extend(phones)
        else:
            conditions.append("1 = 0")

    if search:
        conditions.append("text LIKE ?")
        params.append(f"%{search}%")

    if chat_id:
        conditions.append("chat_id = ?")
        params.append(chat_id)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    total = conn.execute(f"SELECT COUNT(*) FROM messages {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM messages {where} ORDER BY sent_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()

    messages = _serialize_messages(conn, rows)
    conn.close()
    return {"total": total, "offset": offset, "limit": limit, "messages": messages}


def _serialize_messages(conn, rows):
    """Turn raw message rows into the chat-bubble shape (with attachments resolved)."""
    attach_rowids = [r["rowid"] for r in rows if r["has_attachment"]]
    attach_map = {}
    if attach_rowids:
        placeholders = ",".join("?" * len(attach_rowids))
        for a in conn.execute(
            f"SELECT message_rowid, idx, path, mime FROM attachments "
            f"WHERE message_rowid IN ({placeholders}) ORDER BY message_rowid, idx",
            attach_rowids,
        ).fetchall():
            mime = a["mime"] or (mimetypes.guess_type(a["path"])[0] if a["path"] else None)
            attach_map.setdefault(a["message_rowid"], []).append({"idx": a["idx"], "mime": mime})

    out = []
    for r in rows:
        resolved_name = "Maxim" if r["is_from_me"] else PHONE_TO_NAME.get(r["phone"], r["phone"] or "Unknown")
        text = (r["text"] or "").lstrip("￼").strip() or None
        attach_path = r["attachment_path"]
        attach_mime = mimetypes.guess_type(attach_path)[0] if attach_path else None
        # Prefer the attachments table; fall back to the legacy single column.
        attachments = attach_map.get(r["rowid"])
        if not attachments and r["has_attachment"] and attach_path:
            attachments = [{"idx": 0, "mime": attach_mime}]
        out.append({
            "rowid": r["rowid"],
            "phone": r["phone"],
            "name": resolved_name,
            "text": text,
            "sent_at": r["sent_at"],
            "is_from_me": r["is_from_me"],
            "has_attachment": r["has_attachment"],
            "attachment_path": attach_path,
            "attachment_mime": attach_mime,
            "attachments": attachments or [],
            "is_reaction": r["is_reaction"],
        })
    return out


@app.get("/messages/context/{rowid}")
def messages_context(rowid: int, before: int = 14, after: int = 14):
    """The conversation around a given message (same chat), for 'jump to this photo'."""
    conn = get_messages_db()
    target = conn.execute(
        "SELECT rowid, sent_at, chat_id FROM messages WHERE rowid = ?", (rowid,)).fetchone()
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="No such message")
    chat_id, sent_at = target["chat_id"], target["sent_at"]
    chat_clause = "chat_id = ?" if chat_id is not None else "chat_id IS NULL"
    chat_params = [chat_id] if chat_id is not None else []
    keep = "is_reaction = 0 AND (text IS NOT NULL OR has_attachment = 1)"

    before_rows = conn.execute(
        f"SELECT * FROM messages WHERE {chat_clause} AND {keep} "
        f"AND (sent_at < ? OR (sent_at = ? AND rowid < ?)) "
        f"ORDER BY sent_at DESC, rowid DESC LIMIT ?",
        chat_params + [sent_at, sent_at, rowid, before]).fetchall()
    after_rows = conn.execute(
        f"SELECT * FROM messages WHERE {chat_clause} AND {keep} "
        f"AND (sent_at > ? OR (sent_at = ? AND rowid > ?)) "
        f"ORDER BY sent_at ASC, rowid ASC LIMIT ?",
        chat_params + [sent_at, sent_at, rowid, after]).fetchall()
    target_row = conn.execute("SELECT * FROM messages WHERE rowid = ?", (rowid,)).fetchone()

    rows = list(reversed(before_rows)) + [target_row] + list(after_rows)
    messages = _serialize_messages(conn, rows)
    conn.close()
    for m in messages:
        m["is_target"] = (m["rowid"] == rowid)
    label = {"chat313739884378608609": "Main", "chat247636595391927399": "OG",
             "chat26176758262309627": "OG (oldest)"}.get(chat_id, chat_id or "—")
    return {"target_rowid": rowid, "chat_id": chat_id, "chat_label": label, "messages": messages}


def _serve_attachment(rowid: int, idx: int):
    conn = get_messages_db()
    row = conn.execute(
        "SELECT path FROM attachments WHERE message_rowid = ? AND idx = ?", (rowid, idx)
    ).fetchone()
    path_rel = row["path"] if row else None
    # Fall back to the legacy single-attachment column (idx 0 only).
    if path_rel is None and idx == 0:
        legacy = conn.execute(
            "SELECT attachment_path FROM messages WHERE rowid = ?", (rowid,)
        ).fetchone()
        path_rel = legacy["attachment_path"] if legacy else None
    conn.close()
    if not path_rel:
        raise HTTPException(status_code=404, detail="No attachment")
    path = os.path.join(DATA_DIR, path_rel)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


THUMBS_DIR = os.path.join(DATA_DIR, "thumbs")
THUMB_MAX = (640, 640)


@app.get("/thumb/{rowid}/{idx}")
def get_thumb(rowid: int, idx: int):
    """Cached small JPEG for the feed. Photos are 2-3MB each; sending thousands of
    full-size ones starves the browser's connections and images fail to load."""
    conn = get_messages_db()
    row = conn.execute(
        "SELECT path FROM attachments WHERE message_rowid = ? AND idx = ?", (rowid, idx)
    ).fetchone()
    path_rel = row["path"] if row else None
    if path_rel is None and idx == 0:
        legacy = conn.execute("SELECT attachment_path FROM messages WHERE rowid = ?", (rowid,)).fetchone()
        path_rel = legacy["attachment_path"] if legacy else None
    conn.close()
    if not path_rel:
        raise HTTPException(status_code=404, detail="No attachment")
    src = os.path.join(DATA_DIR, path_rel)
    if not os.path.exists(src):
        raise HTTPException(status_code=404, detail="File not found")
    if (mimetypes.guess_type(src)[0] or "").startswith("video/"):
        return FileResponse(src)  # videos stream as-is

    os.makedirs(THUMBS_DIR, exist_ok=True)
    dst = os.path.join(THUMBS_DIR, f"{rowid}_{idx}.jpg")
    if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
        try:
            from PIL import Image, ImageOps
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail(THUMB_MAX)
                im.convert("RGB").save(dst + ".tmp", "JPEG", quality=78, optimize=True)
            os.replace(dst + ".tmp", dst)
        except Exception:
            return FileResponse(src)  # animated GIF, odd format, etc.
    return FileResponse(dst, media_type="image/jpeg")


@app.get("/attachment/{rowid}")
def get_attachment(rowid: int):
    return _serve_attachment(rowid, 0)


@app.get("/attachment/{rowid}/{idx}")
def get_attachment_idx(rowid: int, idx: int):
    return _serve_attachment(rowid, idx)


# ─── Photo<->drink review queue ─────────────────────────────────────────────────

def get_match_db():
    if not os.path.exists(MATCH_DB):
        raise HTTPException(status_code=503,
                            detail="photo_match.db missing — run scripts/match_photos.py")
    conn = sqlite3.connect(MATCH_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _drink_oi(conn, drink_id):
    if drink_id is None:
        return None
    row = conn.execute("SELECT order_index FROM drinks WHERE drink_id = ?", (drink_id,)).fetchone()
    return row["order_index"] if row else None


def _covered_rows(conn, covered_ids, pointer_drink_id=None, order_index=None, before=3, after=28):
    """Drinks covered by a photo (highlighted) plus a generous scrollable window.

    Rows already assigned by a CONFIRMED photo are dropped ("popped out of the queue") so the
    list only shows the current photo's rows plus rows still up for grabs. Covered rows may be
    non-contiguous (deferred or pinned-away rows sit between them).
    """
    covered = set(covered_ids or [])
    # One pass over the queue: map every assigned drink -> its photo, and collect drinks
    # already locked in by OTHER confirmed photos (those get popped out of the list).
    done = set()
    photo_of = {}
    for oi, arowid, aidx, cids, status in conn.execute(
        "SELECT order_index, attachment_rowid, attachment_idx, covered_ids, status "
        "FROM photo_queue WHERE covered_ids IS NOT NULL AND covered_ids != '[]' "
        "ORDER BY order_index"):
        ids = json.loads(cids)
        for did in ids:
            photo_of[did] = f"/attachment/{arowid}/{aidx}"   # later photo wins
        if status in ("confirmed", "non_drink") and oi != order_index:
            done.update(ids)

    anchors = [oi for oi in (_drink_oi(conn, d) for d in covered) if oi is not None]
    ptr_oi = _drink_oi(conn, pointer_drink_id)
    if ptr_oi is not None:
        anchors.append(ptr_oi)
    if not anchors:
        return []
    start_oi = max(0, min(anchors) - before)
    want = before + after + 1
    rows = conn.execute(
        "SELECT order_index, drink_id, drinker, type, info, date_str, people, event, location, deferred "
        "FROM drinks WHERE order_index >= ? ORDER BY order_index LIMIT ?",
        (start_oi, want + 60)).fetchall()
    out = []
    for r in rows:
        is_cov = r["drink_id"] in covered
        if (r["drink_id"] in done) and not is_cov:
            continue                         # assigned elsewhere → popped out
        d = dict(r)
        d["covered"] = is_cov
        d["pointer"] = (not covered) and r["drink_id"] == pointer_drink_id
        d["photo"] = photo_of.get(r["drink_id"])
        out.append(d)
        if len(out) >= want:
            break
    return out


def _item_payload(conn, r):
    span = r["assigned_span"] if r["assigned_span"] is not None else r["suggested_span"]
    return {
        "order_index": r["order_index"],
        "attachment_rowid": r["attachment_rowid"],
        "attachment_idx": r["attachment_idx"],
        "attachment_url": f"/attachment/{r['attachment_rowid']}/{r['attachment_idx']}",
        "attachment_file": r["attachment_file"],
        "mime": r["mime"],
        "sent_at": r["sent_at"],
        "local_date": r["local_date"],
        "msg_text": r["msg_text"],
        "numbers": json.loads(r["parsed_numbers"] or "[]"),
        "suggested_span": r["suggested_span"],
        "assigned_span": r["assigned_span"],
        "effective_span": span,
        "skip_before": r["skip_before"],
        "start_drink_id": r["start_drink_id"],
        "pointer_drink_id": r["pointer_drink_id"],
        "pinned": r["pinned_ids"] is not None,
        "pinned_ids": json.loads(r["pinned_ids"]) if r["pinned_ids"] is not None else None,
        "covered_ids": json.loads(r["covered_ids"] or "[]"),
        "status": r["status"],
        "confidence": r["confidence"],
        "notes": r["notes"],
    }


@app.get("/review/rows")
def review_rows():
    """Every drink row (the full log), each with its ASSIGNED photo if a confirmed photo
    covers it. Auto-flow guesses do NOT get a thumbnail — only real assignments."""
    conn = get_match_db()
    assigned, order_of = {}, {}
    for oi, arowid, aidx, cids in conn.execute(
        "SELECT order_index, attachment_rowid, attachment_idx, covered_ids FROM photo_queue "
        "WHERE status = 'confirmed' AND covered_ids IS NOT NULL AND covered_ids != '[]' "
        "ORDER BY order_index"):
        for did in json.loads(cids):
            assigned[did] = f"/attachment/{arowid}/{aidx}"   # later confirmed photo wins
            order_of[did] = oi                                # queue position to open on click
    rows = conn.execute(
        "SELECT order_index, drink_id, drinker, type, info, date_str, people, event, location, deferred "
        "FROM drinks ORDER BY order_index").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["photo"] = assigned.get(r["drink_id"])
        d["photo_order"] = order_of.get(r["drink_id"])
        d["confirmed"] = r["drink_id"] in assigned
        out.append(d)
    return {"rows": out}


@app.get("/review/item/{order_index}")
def review_item(order_index: int):
    conn = get_match_db()
    r = conn.execute("SELECT * FROM photo_queue WHERE order_index = ?", (order_index,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(status_code=404, detail="No such queue item")
    payload = _item_payload(conn, r)
    total = conn.execute("SELECT COUNT(*) FROM photo_queue").fetchone()[0]
    conn.close()
    payload["total"] = total
    payload["has_prev"] = order_index > 0
    payload["has_next"] = order_index < total - 1
    return payload


class ReviewUpdate(BaseModel):
    assigned_span: Optional[int] = None
    skip_before: Optional[int] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    pinned_ids: Optional[List[int]] = None   # set the explicit set of drinks (out of flow)
    clear_pins: Optional[bool] = None        # true -> back to flow (pinned_ids = NULL)


@app.post("/review/item/{order_index}")
def review_update(order_index: int, body: ReviewUpdate):
    conn = get_match_db()
    r = conn.execute("SELECT * FROM photo_queue WHERE order_index = ?", (order_index,)).fetchone()
    if not r:
        conn.close()
        raise HTTPException(status_code=404, detail="No such queue item")
    sets, params = [], []
    if body.assigned_span is not None:
        sets.append("assigned_span = ?"); params.append(max(0, body.assigned_span))
    if body.skip_before is not None:
        sets.append("skip_before = ?"); params.append(max(0, body.skip_before))
    if body.status is not None:
        sets.append("status = ?"); params.append(body.status)
    if body.notes is not None:
        sets.append("notes = ?"); params.append(body.notes)
    if body.clear_pins:
        sets.append("pinned_ids = NULL")
    elif body.pinned_ids is not None:
        ids = sorted(set(body.pinned_ids), reverse=True)   # store in chronological (id-desc) order
        if ids:
            found = {r[0] for r in conn.execute(
                f"SELECT drink_id FROM drinks WHERE drink_id IN ({','.join('?'*len(ids))})", ids)}
            missing = [i for i in ids if i not in found]
            if missing:
                conn.close()
                raise HTTPException(status_code=400, detail=f"No drink(s): {missing}")
            # Pinning held rows resolves them.
            conn.execute(
                f"UPDATE drinks SET deferred = 0 WHERE drink_id IN ({','.join('?'*len(ids))})", ids)
        sets.append("pinned_ids = ?"); params.append(json.dumps(ids))
    if sets:
        params.append(order_index)
        conn.execute(f"UPDATE photo_queue SET {', '.join(sets)} WHERE order_index = ?", params)
        conn.commit()
        match_photos.recompute(conn)   # downstream start_drink_id may shift
    r = conn.execute("SELECT * FROM photo_queue WHERE order_index = ?", (order_index,)).fetchone()
    payload = _item_payload(conn, r)
    total = conn.execute("SELECT COUNT(*) FROM photo_queue").fetchone()[0]
    conn.close()
    payload["total"] = total
    payload["has_prev"] = order_index > 0
    payload["has_next"] = order_index < total - 1
    return payload


@app.get("/review/progress")
def review_progress():
    conn = get_match_db()
    total = conn.execute("SELECT COUNT(*) FROM photo_queue").fetchone()[0]
    confirmed = conn.execute(
        "SELECT COUNT(*) FROM photo_queue WHERE status IN ('confirmed','non_drink')").fetchone()[0]
    total_drinks = conn.execute("SELECT COUNT(*) FROM drinks").fetchone()[0]
    deferred = conn.execute("SELECT COUNT(*) FROM drinks WHERE deferred = 1").fetchone()[0]
    # distinct drinks actually covered, from the materialized covered_ids
    covered_drinks = set()
    for (cids,) in conn.execute("SELECT covered_ids FROM photo_queue WHERE covered_ids IS NOT NULL"):
        covered_drinks.update(json.loads(cids))
    photos_covering = conn.execute(
        "SELECT COUNT(*) FROM photo_queue WHERE covered_ids IS NOT NULL AND covered_ids != '[]'").fetchone()[0]
    conn.close()
    unassigned = total_drinks - len(covered_drinks) - deferred
    return {
        "total_photos": total,
        "reviewed": confirmed,
        "total_drinks": total_drinks,
        "drinks_covered": len(covered_drinks),
        "drinks_deferred": deferred,
        "rows_unaccounted": unassigned,        # not covered, not deferred (blank)
        "photos_covering_rows": photos_covering,
    }


@app.get("/review/deferred")
def review_deferred():
    """Rows held out of the flow ('at the top of the queue'), awaiting a photo."""
    conn = get_match_db()
    rows = conn.execute(
        "SELECT order_index, drink_id, drinker, type, info, date_str, people, event, location "
        "FROM drinks WHERE deferred = 1 ORDER BY order_index").fetchall()
    conn.close()
    return [dict(r) for r in rows]


class DeferUpdate(BaseModel):
    deferred: bool


@app.post("/review/defer/{drink_id}")
def review_defer(drink_id: int, body: DeferUpdate):
    conn = get_match_db()
    if not conn.execute("SELECT 1 FROM drinks WHERE drink_id = ?", (drink_id,)).fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail=f"No drink #{drink_id}")
    conn.execute("UPDATE drinks SET deferred = ? WHERE drink_id = ?",
                 (1 if body.deferred else 0, drink_id))
    conn.commit()
    match_photos.recompute(conn)
    conn.close()
    return {"drink_id": drink_id, "deferred": body.deferred}


@app.get("/review/export.csv")
def review_export_csv():
    """Stream the augmented CSV as a browser download (avoids daemon filesystem perms)."""
    if not os.path.exists(MATCH_DB):
        raise HTTPException(status_code=503, detail="photo_match.db missing")
    text, filled, mapped = match_photos.export_csv_text(match_db=MATCH_DB)
    from fastapi.responses import Response
    return Response(content=text, media_type="text/csv", headers={
        "Content-Disposition": "attachment; filename=latest_with_photos.csv",
        "X-Rows-With-Photo": str(filled), "X-Drinks-Mapped": str(mapped),
    })


@app.post("/review/export")
def review_export():
    """Write the CSV to disk (works from the CLI / a process with Desktop access)."""
    if not os.path.exists(MATCH_DB):
        raise HTTPException(status_code=503, detail="photo_match.db missing")
    try:
        out, filled, mapped = match_photos.export_csv(match_db=MATCH_DB)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not write file: {e}")
    return {"output": out, "rows_with_photo": filled, "drinks_mapped": mapped}


# ─── Message groups + notes (R&D annotations) ─────────────────────────────────
# Kept in their own DB so sync_messages.py never touches them. A message
# belongs to at most one group.

ANNOTATIONS_DB = os.path.expanduser("~/drinks/data/annotations.db")


def get_annotations_db():
    conn = sqlite3.connect(ANNOTATIONS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    TEXT,
            note       TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS group_messages (
            message_rowid INTEGER PRIMARY KEY,
            group_id      INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_gm_group ON group_messages(group_id);
    """)
    # solo = the photo is a true solo (only the drinker in it)
    try:
        conn.execute("ALTER TABLE groups ADD COLUMN solo INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # set_id = independent annotation pass (1, 2, ...); a message may belong to
    # one group *per set*, so group_messages is keyed by (set_id, message_rowid).
    try:
        conn.execute("ALTER TABLE groups ADD COLUMN set_id INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # idx = which attachment of that message; 0 for the message itself / first photo.
    # Lets each photo of a multi-photo message carry its own entry.
    gm_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='group_messages'"
    ).fetchone()
    if gm_sql and "idx" not in gm_sql[0]:
        conn.executescript("""
            CREATE TABLE group_messages_idx (
                set_id        INTEGER NOT NULL DEFAULT 1,
                message_rowid INTEGER NOT NULL,
                idx           INTEGER NOT NULL DEFAULT 0,
                group_id      INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                PRIMARY KEY (set_id, message_rowid, idx)
            );
            INSERT INTO group_messages_idx (set_id, message_rowid, idx, group_id)
                SELECT set_id, message_rowid, 0, group_id FROM group_messages;
            DROP TABLE group_messages;
            ALTER TABLE group_messages_idx RENAME TO group_messages;
            CREATE INDEX IF NOT EXISTS idx_gm_group ON group_messages(group_id);
        """)
        conn.commit()
    try:
        conn.execute("ALTER TABLE group_messages ADD COLUMN detached INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    old_pk = gm_sql
    if old_pk and "set_id" not in old_pk[0]:
        conn.executescript("""
            CREATE TABLE group_messages_new (
                set_id        INTEGER NOT NULL DEFAULT 1,
                message_rowid INTEGER NOT NULL,
                group_id      INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                PRIMARY KEY (set_id, message_rowid)
            );
            INSERT INTO group_messages_new (set_id, message_rowid, group_id)
                SELECT g.set_id, gm.message_rowid, gm.group_id
                FROM group_messages gm JOIN groups g ON g.id = gm.group_id;
            DROP TABLE group_messages;
            ALTER TABLE group_messages_new RENAME TO group_messages;
            CREATE INDEX IF NOT EXISTS idx_gm_group ON group_messages(group_id);
        """)
        conn.commit()
    return conn


class GroupCreate(BaseModel):
    set_id: int = 1
    chat_id: Optional[str] = None
    rowids: List[int] = []                    # whole messages (idx 0)
    photos: List[List[int]] = []              # [[message_rowid, idx], ...]
    note: str = ""
    solo: bool = False


class GroupUpdate(BaseModel):
    note: Optional[str] = None
    solo: Optional[bool] = None
    add_rowids: List[int] = []
    remove_rowids: List[int] = []
    add_photos: List[List[int]] = []
    remove_photos: List[List[int]] = []


def _note_drinks(note):
    """Total drinks in a note: sum of the amount in each `person, amount, drink` line."""
    total = 0
    for line in (note or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[1].isdigit():
            total += int(parts[1])
    return total


def _entry_labels(conn, set_id=1):
    """Label each group with the running drink total, counting newest entry first.

    Mirrors the countdown: an entry covering the 6th-8th annotated drinks is #8.
    Entries whose note has no drinks yet get None (shown/exported as unlabeled).
    """
    conn.execute("ATTACH DATABASE ? AS msg", (MESSAGES_DB,))
    rows = conn.execute("""
        SELECT g.id, g.note, MAX(m.sent_at) AS last_at
        FROM groups g
        JOIN group_messages gm ON gm.group_id = g.id
        LEFT JOIN msg.messages m ON m.rowid = gm.message_rowid
        WHERE g.set_id = ?
        GROUP BY g.id
        ORDER BY last_at DESC, g.id DESC
    """, (set_id,)).fetchall()
    conn.execute("DETACH DATABASE msg")
    labels, running = {}, 0
    for gid, note, _ in rows:
        n = _note_drinks(note)
        running += n
        labels[gid] = running if n else None
    return labels


def _group_payload(conn, group_id, labels=None):
    g = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    if not g:
        return None
    members = conn.execute(
        "SELECT message_rowid, idx FROM group_messages WHERE group_id = ? ORDER BY message_rowid, idx",
        (group_id,)).fetchall()
    photos = [[m[0], m[1]] for m in members]
    rowids = list(dict.fromkeys(m[0] for m in members))
    if labels is None:
        labels = _entry_labels(conn, g["set_id"])
    return {**dict(g), "rowids": rowids, "photos": photos, "label": labels.get(group_id)}


def _as_pairs(rowids=None, photos=None):
    """Normalise membership input to (message_rowid, idx) pairs; bare ids mean idx 0."""
    pairs = [(int(r), 0) for r in (rowids or [])]
    pairs += [(int(p[0]), int(p[1])) for p in (photos or [])]
    return list(dict.fromkeys(pairs))


def _attach_rowids(conn, group_id, rowids=None, photos=None):
    # Moving a photo into this group pulls it out of any other group in the same set.
    set_id = conn.execute("SELECT set_id FROM groups WHERE id = ?", (group_id,)).fetchone()[0]
    conn.executemany(
        "INSERT INTO group_messages (set_id, message_rowid, idx, group_id) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(set_id, message_rowid, idx) DO UPDATE SET group_id = excluded.group_id, "
        "detached = 0",
        [(set_id, r, i, group_id) for r, i in _as_pairs(rowids, photos)])
    # Drop groups left empty by the move.
    conn.execute("DELETE FROM groups WHERE id NOT IN (SELECT DISTINCT group_id FROM group_messages)")


@app.get("/groups")
def list_groups(chat_id: str = None, set_id: int = 1):
    conn = get_annotations_db()
    where, params = ["set_id = ?"], [set_id]
    if chat_id:
        where.append("chat_id = ?")
        params.append(chat_id)
    groups = conn.execute(
        f"SELECT * FROM groups WHERE {' AND '.join(where)} ORDER BY id", params).fetchall()
    # one membership query for the whole set (set 3 has ~4k groups)
    members = {}
    for gid, rowid, idx in conn.execute(
            "SELECT group_id, message_rowid, idx FROM group_messages WHERE set_id = ? "
            "ORDER BY group_id, message_rowid, idx", (set_id,)):
        members.setdefault(gid, []).append((rowid, idx))
    labels = _entry_labels(conn, set_id)
    out = [{**dict(g),
            "rowids": list(dict.fromkeys(r for r, _ in members.get(g["id"], []))),
            "photos": [[r, i] for r, i in members.get(g["id"], [])],
            "label": labels.get(g["id"])}
           for g in groups]
    conn.close()
    return out


@app.post("/groups")
def create_group(body: GroupCreate):
    if not (body.rowids or body.photos):
        raise HTTPException(status_code=400, detail="rowids or photos required")
    conn = get_annotations_db()
    cur = conn.execute("INSERT INTO groups (set_id, chat_id, note, solo) VALUES (?, ?, ?, ?)",
                       (body.set_id, body.chat_id, body.note, 1 if body.solo else 0))
    gid = cur.lastrowid
    _attach_rowids(conn, gid, body.rowids, body.photos)
    conn.commit()
    out = _group_payload(conn, gid)
    conn.close()
    return out


@app.patch("/groups/{group_id}")
def update_group(group_id: int, body: GroupUpdate):
    conn = get_annotations_db()
    if not conn.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,)).fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="group not found")
    if body.note is not None:
        conn.execute("UPDATE groups SET note = ?, updated_at = datetime('now') WHERE id = ?",
                     (body.note, group_id))
    if body.solo is not None:
        conn.execute("UPDATE groups SET solo = ?, updated_at = datetime('now') WHERE id = ?",
                     (1 if body.solo else 0, group_id))
    removals = _as_pairs(body.remove_rowids, body.remove_photos)
    if removals:
        conn.executemany(
            "DELETE FROM group_messages WHERE group_id = ? AND message_rowid = ? AND idx = ?",
            [(group_id, r, i) for r, i in removals])
    if body.add_rowids or body.add_photos:
        _attach_rowids(conn, group_id, body.add_rowids, body.add_photos)
    conn.execute("DELETE FROM groups WHERE id NOT IN (SELECT DISTINCT group_id FROM group_messages)")
    conn.commit()
    out = _group_payload(conn, group_id)
    conn.close()
    return out or {"id": group_id, "deleted": True}


@app.delete("/groups/{group_id}")
def delete_group(group_id: int):
    conn = get_annotations_db()
    conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    conn.commit()
    conn.close()
    return {"id": group_id, "deleted": True}


@app.get("/groups/export.csv")
def export_annotated_messages(chat_id: str = None, set_id: int = 1):
    """Every messages.db row, columns unchanged, plus entry_id + annotation.

    Rows in the same group share one entry_id/annotation (one entry = many rows).
    """
    import csv
    import io
    from fastapi.responses import Response

    ann = get_annotations_db()
    labels = _entry_labels(ann, set_id)
    ann.close()
    conn = get_messages_db()
    conn.execute("ATTACH DATABASE ? AS ann", (ANNOTATIONS_DB,))
    where, params = ("WHERE m.chat_id = ?", [chat_id]) if chat_id else ("", [])
    cur = conn.execute(f"""
        SELECT m.*,
               GROUP_CONCAT(g.id) AS entry_id,
               GROUP_CONCAT(g.note, '\n') AS annotation,
               MAX(g.solo) AS solo
        FROM messages m
        LEFT JOIN ann.group_messages gm ON gm.message_rowid = m.rowid AND gm.set_id = {int(set_id)}
        LEFT JOIN ann.groups g ON g.id = gm.group_id
        {where}
        GROUP BY m.rowid
        ORDER BY m.sent_at, m.rowid
    """, params)
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = [d[0] for d in cur.description]
    w.writerow(cols)

    def clean(v):
        # a few messages carry stray control bytes from attributedBody; they make the
        # CSV unreadable to strict parsers (_csv.Error: line contains NUL)
        if isinstance(v, str):
            return "".join(ch for ch in v if ch >= " " or ch in "\t\n")
        return v

    gi = cols.index("entry_id")
    si = cols.index("solo")
    for row in cur:
        row = [clean(v) for v in row]
        if row[gi] is not None:
            # entry_id = running drink total (see _entry_labels), not the internal group id;
            # a multi-photo message carries one entry per photo, joined by ";"
            out = []
            for gid in str(row[gi]).split(","):
                lab = labels.get(int(gid))
                out.append(str(lab) if lab is not None else f"unlabeled-{gid}")
            row[gi] = ";".join(out)
            row[si] = "true" if row[si] else "false"
        w.writerow(row)
    conn.close()
    name = f"messages_annotated_set{set_id}{'_' + chat_id if chat_id else ''}.csv"
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.get("/groups/sets")
def list_sets():
    conn = get_annotations_db()
    rows = conn.execute(
        "SELECT set_id, COUNT(*) AS groups FROM groups GROUP BY set_id ORDER BY set_id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/groups/sets/{dst}/copy-from/{src}")
def copy_set(dst: int, src: int):
    """Duplicate every group of set `src` into set `dst` (refuses if dst exists)."""
    conn = get_annotations_db()
    if conn.execute("SELECT 1 FROM groups WHERE set_id = ?", (dst,)).fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail=f"set {dst} already has annotations")
    copied = 0
    for g in conn.execute("SELECT * FROM groups WHERE set_id = ? ORDER BY id", (src,)).fetchall():
        cur = conn.execute(
            "INSERT INTO groups (set_id, chat_id, note, solo, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (dst, g["chat_id"], g["note"], g["solo"], g["created_at"], g["updated_at"]))
        conn.execute(
            "INSERT INTO group_messages (set_id, message_rowid, group_id) "
            "SELECT ?, message_rowid, ? FROM group_messages WHERE group_id = ?",
            (dst, cur.lastrowid, g["id"]))
        copied += 1
    conn.commit()
    conn.close()
    return {"copied": copied, "from": src, "to": dst}


# ─── Photo-grid validation view ───────────────────────────────────────────────

class BulkIds(BaseModel):
    ids: List[int]


class BulkGroups(BaseModel):
    groups: List[GroupCreate]


@app.get("/grid")
def grid(set_id: int = 3, only: str = "annotated", chat_id: str = None):
    """One row per photo/video attachment with its annotation (if any).

    Feeds the validation grid, which shows photos only — no message history — so
    the page never has to load 15k chat rows.
    """
    ann = get_annotations_db()
    labels = _entry_labels(ann, set_id)
    ann.close()

    conn = get_messages_db()
    conn.execute("ATTACH DATABASE ? AS ann", (ANNOTATIONS_DB,))
    where, params = ["1 = 1"], [set_id]
    if chat_id:
        where.append("m.chat_id = ?")
        params.append(chat_id)
    if only == "annotated":
        where.append("g.id IS NOT NULL")
    elif only == "unannotated":
        where.append("g.id IS NULL")

    rows = conn.execute(f"""
        SELECT a.message_rowid AS rowid, a.idx AS idx, a.path AS path, a.mime AS mime,
               m.sent_at, m.chat_id, m.text, m.is_from_me, m.phone,
               g.id AS entry_id, g.note AS note, g.solo AS solo
        FROM attachments a
        JOIN messages m ON m.rowid = a.message_rowid
        LEFT JOIN ann.group_messages gm
               ON gm.message_rowid = a.message_rowid AND gm.idx = a.idx
              AND gm.set_id = ? AND gm.detached = 0
        LEFT JOIN ann.groups g ON g.id = gm.group_id
        WHERE {' AND '.join(where)}
        ORDER BY m.sent_at DESC, a.idx
    """, params).fetchall()

    counts = dict(conn.execute("""
        SELECT CASE WHEN gm.group_id IS NULL THEN 'unannotated' ELSE 'annotated' END, COUNT(*)
        FROM attachments a
        JOIN messages m ON m.rowid = a.message_rowid
        LEFT JOIN ann.group_messages gm
               ON gm.message_rowid = a.message_rowid AND gm.idx = a.idx
              AND gm.set_id = ? AND gm.detached = 0
        GROUP BY 1
    """, (set_id,)).fetchall())
    conn.close()

    out = []
    for r in rows:
        out.append({
            "rowid": r["rowid"], "idx": r["idx"],
            "sent_at": r["sent_at"], "chat_id": r["chat_id"],
            "text": (r["text"] or "").lstrip("￼").strip() or None,
            "name": "Maxim" if r["is_from_me"] else PHONE_TO_NAME.get(r["phone"], r["phone"] or "Unknown"),
            "is_video": (r["mime"] or "").startswith("video/"),
            "has_file": bool(r["path"]) and os.path.exists(os.path.join(DATA_DIR, r["path"])),
            "entry_id": r["entry_id"], "note": r["note"], "solo": r["solo"],
            "label": labels.get(r["entry_id"]),
        })
    return {"set_id": set_id, "only": only, "counts": counts, "photos": out}


@app.post("/groups/bulk-delete")
def bulk_delete_groups(body: BulkIds):
    """Delete several entries at once; returns what they held so the UI can undo."""
    conn = get_annotations_db()
    undo = []
    for gid in body.ids:
        g = conn.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
        if not g:
            continue
        photos = [[r[0], r[1]] for r in conn.execute(
            "SELECT message_rowid, idx FROM group_messages WHERE group_id = ?", (gid,))]
        undo.append({"set_id": g["set_id"], "chat_id": g["chat_id"], "note": g["note"],
                     "solo": bool(g["solo"]), "photos": photos})
        conn.execute("DELETE FROM groups WHERE id = ?", (gid,))
    conn.commit()
    conn.close()
    return {"deleted": len(undo), "undo": undo}


@app.post("/groups/bulk")
def bulk_create_groups(body: BulkGroups):
    """Create several entries at once (used by the grid's undo)."""
    conn = get_annotations_db()
    made = []
    for b in body.groups:
        if not (b.rowids or b.photos):
            continue
        cur = conn.execute(
            "INSERT INTO groups (set_id, chat_id, note, solo) VALUES (?, ?, ?, ?)",
            (b.set_id, b.chat_id, b.note, 1 if b.solo else 0))
        _attach_rowids(conn, cur.lastrowid, b.rowids, b.photos)
        made.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return {"created": len(made), "ids": made}


@app.get("/grid/export.csv")
def export_photo_annotations(set_id: int = 3, only: str = "annotated", chat_id: str = None):
    """One row per photo: the file, who sent it, and its annotation. No chat history."""
    import csv
    import io
    from fastapi.responses import Response

    data = grid(set_id=set_id, only=only, chat_id=chat_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["photo", "message_rowid", "photo_idx", "sent_at", "sender", "chat_id",
                "message_text", "entry_id", "annotation", "solo", "file_present"])
    for p in data["photos"]:
        suffix = "" if p["idx"] == 0 else f"_{p['idx']}"
        label = p["label"] if p["label"] is not None else (
            f"unlabeled-{p['entry_id']}" if p["entry_id"] else "")
        w.writerow([f"{p['rowid']}{suffix}", p["rowid"], p["idx"], p["sent_at"], p["name"],
                    p["chat_id"], p["text"] or "", label, p["note"] or "",
                    "true" if p["solo"] else ("false" if p["entry_id"] else ""),
                    "true" if p["has_file"] else "false"])
    return Response(content=buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": f"attachment; filename=photo_annotations_set{set_id}.csv"})


class PhotoLinks(BaseModel):
    set_id: int = 3
    photos: List[List[int]]            # [[message_rowid, idx], ...]
    detached: bool = True


@app.post("/grid/unlink")
def unlink_photos(body: PhotoLinks):
    """Break the photo -> annotation link without touching the annotation itself.

    The grid's delete: the photo drops out of the grid (and the photo CSV), its
    siblings keep their own annotations, and the entry stays attached to the
    message so the chat export still carries the note. detached=False re-links.
    """
    conn = get_annotations_db()
    flag = 1 if body.detached else 0
    changed = conn.executemany(
        "UPDATE group_messages SET detached = ? WHERE set_id = ? AND message_rowid = ? AND idx = ?",
        [(flag, body.set_id, r, i) for r, i in _as_pairs(photos=body.photos)])
    conn.commit()
    n = conn.execute(
        "SELECT COUNT(*) FROM group_messages WHERE set_id = ? AND detached = 1", (body.set_id,)
    ).fetchone()[0]
    conn.close()
    return {"photos": len(body.photos), "detached": body.detached, "total_unlinked": n}
