"""
Simulate group chat message sequences to test the watcher without touching
the real chat.db or drinks.db.

Usage:
    python watcher/simulate.py              # run built-in test scenarios
    python watcher/simulate.py --interactive # REPL to inject messages manually

Interactive commands:
    p              photo only
    p 7001         photo + number (Case 1)
    p 7001 modelo  photo + number + details
    n 7001         number only
    n 7001 modelo  number + details
    n 7001-6999    range
    n 7001,7000    comma list
    from <name>    switch active sender (default: Liam)
    wait <secs>    advance the clock (triggers 90s expiry)
    seed <name> <n> pre-set last drink number for a person
    show           print drinks logged so far
    reset          clear pending state and drinks db
    quit / q       exit
"""

import sys
import os
import sqlite3
import time

sys.path.insert(0, os.path.dirname(__file__))

# Patch constants before watcher module-level code runs
os.environ.setdefault("DRINKS_DB_PATH", ":memory:")

import watcher as w
from parser import resolve_name

# ─── Fake time ────────────────────────────────────────────────────────────────

_time_offset = 0.0

_real_time = time.time

def _fake_time():
    return _real_time() + _time_offset

# Patch time.time used inside watcher
import time as _time_mod
_time_mod.time = _fake_time
# Also patch PendingLog's default_factory which captures time.time at import
# We re-assign started_at manually when needed via fast-forward

def advance_clock(seconds):
    global _time_offset
    _time_offset += seconds
    print(f"  ⏩ Clock advanced {seconds}s (total offset: {_time_offset:.0f}s)")


# ─── Apple timestamp ──────────────────────────────────────────────────────────

APPLE_EPOCH = 978307200

def apple_now():
    """Current time as Apple nanosecond timestamp."""
    return int((_real_time() - APPLE_EPOCH) * 1e9)


# ─── In-memory drinks DB ──────────────────────────────────────────────────────

def make_db():
    conn = sqlite3.connect(":memory:")
    w.init_drinks_db(conn)
    return conn

_db = make_db()


def seed_person(conn, person, last_drink_number):
    """Pre-populate drinks.db so validation has a baseline for a person."""
    conn.execute(
        "INSERT OR IGNORE INTO drinks (drink_number, person, details, date, imessage_id, source) "
        "VALUES (?, ?, NULL, '2026-01-01 00:00:00', ?, 'seed')",
        (last_drink_number, person, -last_drink_number)
    )
    conn.commit()
    print(f"  Seeded: {person} last drink = #{last_drink_number}")


def show_drinks(conn):
    rows = conn.execute("SELECT drink_number, person, details, source FROM drinks WHERE imessage_id > 0 ORDER BY drink_number").fetchall()
    if not rows:
        print("  (no drinks logged yet)")
    for r in rows:
        details = f" — {r[2]}" if r[2] else ""
        print(f"  #{r[0]}  {r[1]}{details}  [{r[3]}]")


def reset(conn):
    conn.execute("DELETE FROM drinks WHERE imessage_id > 0")
    conn.commit()
    w.pending.clear()
    global _time_offset
    _time_offset = 0.0
    print("  Reset: pending cleared, drinks wiped, clock reset")


# ─── Message builder ──────────────────────────────────────────────────────────

_rowid_counter = 1000

def next_rowid():
    global _rowid_counter
    _rowid_counter += 1
    return _rowid_counter

SENDERS = {
    "liam":   "+18453002491",
    "hunter": "+17147429858",
    "marek":  "+14083321330",
    "jacob":  "+16179130745",
    "cole":   "+19842608337",
    "maxim":  "+17812050278",
}

def make_msg(sender_name, text=None, has_attachment=False):
    handle = SENDERS.get(sender_name.lower(), f"+1555{hash(sender_name) % 10000000:07d}")
    return (next_rowid(), handle, text, apple_now(), int(has_attachment))

def _resolve(conn):
    """Simulate end-of-tick resolution pass."""
    for s in list(w.pending):
        p = w.pending.get(s)
        if p and p.photos:
            w.try_resolve(s, conn)

def inject(sender, text=None, photo=False, conn=None):
    """Inject a single message as its own poll tick (resolves immediately after)."""
    msg = make_msg(sender, text=text, has_attachment=photo)
    label = []
    if photo: label.append("[photo]")
    if text:  label.append(repr(text))
    print(f"  → {sender}: {' '.join(label) or '(empty)'}")
    w.handle_message(msg, conn or _db)
    _resolve(conn or _db)

def inject_tick(messages, conn=None):
    """Inject multiple messages as one poll tick — resolves once at the end."""
    db = conn or _db
    for sender, kwargs in messages:
        text = kwargs.get('text')
        photo = kwargs.get('photo', False)
        msg = make_msg(sender, text=text, has_attachment=photo)
        label = []
        if photo: label.append("[photo]")
        if text:  label.append(repr(text))
        print(f"  → {sender}: {' '.join(label) or '(empty)'} [same tick]")
        w.handle_message(msg, db)
    _resolve(db)


# ─── Built-in test scenarios ──────────────────────────────────────────────────

def run_scenario(name, fn):
    global _time_offset, _rowid_counter
    print(f"\n{'─'*50}")
    print(f"SCENARIO: {name}")
    print('─'*50)
    w.pending.clear()
    _time_offset = 0.0
    conn = make_db()
    fn(conn)
    w.check_expirations(conn)
    show_drinks(conn)

def _s1(conn):
    "Photo then number (happy path)"
    inject("Liam", photo=True, conn=conn)
    inject("Liam", "7001", conn=conn)
    w.check_expirations(conn)

def _s2(conn):
    "Photo + number in same message"
    inject("Liam", "￼7001 modelo", photo=True, conn=conn)

def _s3(conn):
    "Number before photo"
    inject("Liam", "7001", conn=conn)
    inject("Liam", photo=True, conn=conn)

def _s4(conn):
    "Different sender types the number (attributed to photo sender)"
    inject("Liam", photo=True, conn=conn)
    inject("Hunter", "7001", conn=conn)
    w.check_expirations(conn)

def _s5(conn):
    "Range: 7001-6999 (three drinks)"
    inject("Liam", photo=True, conn=conn)
    inject("Liam", "7001-6999", conn=conn)
    w.check_expirations(conn)

def _s6(conn):
    "Comma list: 7001, 7000, 6999"
    inject("Liam", photo=True, conn=conn)
    inject("Liam", "7001, 7000, 6999", conn=conn)
    w.check_expirations(conn)

def _s7(conn):
    "Photo with no number — expires after 90s"
    inject("Liam", photo=True, conn=conn)
    advance_clock(91)
    w.check_expirations(conn)

def _s8(conn):
    "Number with no photo — expires, flags MISSING_PHOTO"
    inject("Liam", "7001", conn=conn)
    advance_clock(91)
    w.check_expirations(conn)

def _s9(conn):
    "Correction: starred number overrides"
    inject("Liam", photo=True, conn=conn)
    inject("Liam", "6999", conn=conn)
    inject("Liam", photo=True, conn=conn)
    inject("Liam", "7001*", conn=conn)
    w.check_expirations(conn)

def _s10(conn):
    "Unrelated chatter ignored (number doesn't match expected)"
    seed_person(conn, "Liam", 7001)
    inject("Liam", "42 is the answer", conn=conn)   # not a drink number
    inject("Liam", "see you at 5", conn=conn)
    inject("Liam", "7002", photo=True, conn=conn)   # actual log
    w.check_expirations(conn)

def _s11(conn):
    "Same-tick correction: photo, wrong number, starred fix all arrive together"
    inject_tick([
        ("Liam", {"photo": True}),
        ("Liam", {"text": "5583"}),
        ("Liam", {"text": "5593*"}),
    ], conn=conn)
    w.check_expirations(conn)

def _s12(conn):
    "Bad range typo corrected with *endpoint — reconstructs from last logged"
    seed_person(conn, "Liam", 1000)
    inject("Liam", photo=True, conn=conn)
    inject("Liam", "999-959", conn=conn)   # typo: meant 999-996, span too large
    inject("Liam", "996*", conn=conn)       # correct endpoint: fills 999→996
    w.check_expirations(conn)

def _s13(conn):
    "Numbers sent as separate messages after photo — continuation window pairs them"
    seed_person(conn, "Liam", 1000)
    inject("Liam", photo=True, conn=conn)
    inject("Liam", "999", conn=conn)        # resolves, photo stays live
    inject("Liam", "cheers!", conn=conn)    # ignored
    inject("Liam", "998", conn=conn)        # pairs with same photo via continuation
    w.check_expirations(conn)

SCENARIOS = [
    ("photo → number (happy path)", _s1),
    ("photo + number in same message", _s2),
    ("number → photo", _s3),
    ("different sender types the number", _s4),
    ("range 7001-6999", _s5),
    ("comma list 7001, 7000, 6999", _s6),
    ("photo only — expires silently", _s7),
    ("number only — expires, flags MISSING_PHOTO", _s8),
    ("starred correction", _s9),
    ("unrelated chatter filtered by validation", _s10),
    ("same-tick correction overrides wrong number", _s11),
    ("bad range typo corrected with *endpoint", _s12),
    ("separate messages after photo — continuation window", _s13),
]


# ─── Interactive REPL ─────────────────────────────────────────────────────────

def interactive():
    global _db
    sender = "Liam"
    print(__doc__)
    print(f"Active sender: {sender}  (change with 'from <name>')")
    print()

    while True:
        try:
            line = input(f"[{sender}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("quit", "q", "exit"):
            break

        elif cmd == "from":
            sender = arg.strip() or sender
            print(f"  Sender → {sender}")

        elif cmd == "show":
            show_drinks(_db)

        elif cmd == "reset":
            reset(_db)

        elif cmd == "seed":
            # seed <name> <number>
            sp = arg.split()
            if len(sp) == 2:
                seed_person(_db, sp[0].capitalize(), int(sp[1]))
            else:
                print("  Usage: seed <name> <number>")

        elif cmd == "wait":
            try:
                secs = float(arg)
                advance_clock(secs)
                w.check_expirations(_db)
            except ValueError:
                print("  Usage: wait <seconds>")

        elif cmd == "p":
            # p [number] [details]
            inject(sender, text=arg.strip() or None, photo=True, conn=_db)
            w.check_expirations(_db)

        elif cmd == "n":
            if not arg:
                print("  Usage: n <number> [details]")
            else:
                inject(sender, text=arg.strip(), photo=False, conn=_db)
                w.check_expirations(_db)

        else:
            print(f"  Unknown command: {cmd!r}  (type 'quit' to exit)")


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--interactive" in sys.argv or "-i" in sys.argv:
        interactive()
    else:
        for name, fn in SCENARIOS:
            run_scenario(name, fn)
        print(f"\n{'─'*50}")
        print(f"All {len(SCENARIOS)} scenarios complete.")
