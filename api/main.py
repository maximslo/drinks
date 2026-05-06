import sqlite3
import os
import json
import mimetypes
import asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

DRINKS_DB = os.path.expanduser("~/drinks/data/drinks.db")
MESSAGES_DB = os.path.expanduser("~/drinks/data/messages.db")
DATA_DIR = os.path.expanduser("~/drinks/data")

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
        ORDER BY total DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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

@app.get("/messages")
def get_messages(
    limit: int = 100,
    offset: int = 0,
    name: str = None,
    show_reactions: bool = False,
    search: str = None,
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

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    total = conn.execute(f"SELECT COUNT(*) FROM messages {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM messages {where} ORDER BY sent_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()
    conn.close()

    messages = []
    for r in rows:
        resolved_name = "Maxim" if r["is_from_me"] else PHONE_TO_NAME.get(r["phone"], r["phone"] or "Unknown")
        text = (r["text"] or "").lstrip("￼").strip() or None
        attach_path = r["attachment_path"]
        attach_mime = mimetypes.guess_type(attach_path)[0] if attach_path else None
        messages.append({
            "rowid": r["rowid"],
            "phone": r["phone"],
            "name": resolved_name,
            "text": text,
            "sent_at": r["sent_at"],
            "is_from_me": r["is_from_me"],
            "has_attachment": r["has_attachment"],
            "attachment_path": attach_path,
            "attachment_mime": attach_mime,
            "is_reaction": r["is_reaction"],
        })

    return {"total": total, "offset": offset, "limit": limit, "messages": messages}

@app.get("/attachment/{rowid}")
def get_attachment(rowid: int):
    conn = get_messages_db()
    row = conn.execute(
        "SELECT attachment_path FROM messages WHERE rowid = ?", (rowid,)
    ).fetchone()
    conn.close()
    if not row or not row["attachment_path"]:
        raise HTTPException(status_code=404, detail="No attachment")
    path = os.path.join(DATA_DIR, row["attachment_path"])
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)
