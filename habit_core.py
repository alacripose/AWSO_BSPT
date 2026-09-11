#!/usr/bin/env python3
"""AWSO_BSPT habit tracker — pure-stdlib core + CLI.

Fork of simulators/portal_simulator.py: the parent's window-manager chassis was
deleted; the business/operator concepts (capacity modes, delegation, audit)
were distilled into this habit engine. Headless-first for small Linux devices
(solar/battery deployments); the GUI (habit_gui.py) attaches only when a
display exists and shares this exact code for every mutation.
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "0.1.0"

CADENCE_RE = re.compile(r"^(daily|weekdays|weekly:[1-9]\d*|interval:[1-9]\d*h)$")
EFFORTS = ("green", "yellow", "red")
# Operator capacity modes (from the parent portal's design language):
# green = full operations, yellow = limited capacity, red = away/continuity.
MODES = ("green", "yellow", "red")
# Which effort tiers each mode defers. Monotonic: red defers the most.
DEFERRED = {"green": (), "yellow": ("red",), "red": ("yellow", "red")}


def default_db() -> Path:
    return Path(os.environ.get("AWSO_DB", Path(__file__).with_name("habits.db")))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt.astimezone() if dt.tzinfo is None else dt  # naive = local time


def local(dt_or_str) -> datetime:
    dt = parse_ts(dt_or_str) if isinstance(dt_or_str, str) else dt_or_str
    return dt.astimezone()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS habits(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
  cadence TEXT NOT NULL DEFAULT 'daily', effort TEXT NOT NULL DEFAULT 'green',
  category TEXT DEFAULT '', archived INTEGER DEFAULT 0, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkins(
  habit_id INTEGER NOT NULL, ts TEXT NOT NULL,
  source TEXT DEFAULT 'human', note TEXT DEFAULT '',
  FOREIGN KEY(habit_id) REFERENCES habits(id));
CREATE TABLE IF NOT EXISTS readings(
  key TEXT NOT NULL, ts TEXT NOT NULL, value REAL NOT NULL, unit TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS chat(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, sender TEXT NOT NULL,
  text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sos(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, severity TEXT NOT NULL,
  code TEXT NOT NULL, message TEXT NOT NULL, fingerprint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', ack_ts TEXT DEFAULT '',
  ack_by TEXT DEFAULT '');
CREATE UNIQUE INDEX IF NOT EXISTS idx_sos_open_fp ON sos(fingerprint)
  WHERE status='pending';
CREATE TABLE IF NOT EXISTS events(
  ts TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
-- releases: the CI/update channel. One row per build that landed here
-- (git commit on the device repo, or a deployed bundle). Clicking a bubble
-- in the Updates tab shows body + detail; backups carry the whole history.
CREATE TABLE IF NOT EXISTS releases(
  id TEXT PRIMARY KEY,           -- commit hash / build id
  ts TEXT NOT NULL, version TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '', files INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_checkins ON checkins(habit_id, ts);
CREATE INDEX IF NOT EXISTS idx_readings ON readings(key, ts);
CREATE INDEX IF NOT EXISTS idx_events ON events(ts);
CREATE INDEX IF NOT EXISTS idx_releases ON releases(ts);
"""


def connect(db=None) -> sqlite3.Connection:
    path = Path(db) if db else default_db()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # SQLite skill rules: WAL + busy_timeout (concurrent CLI/GUI/agent),
    # foreign_keys ON per connection (never persisted), NORMAL sync w/ WAL.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def log_event(conn, kind, message):
    conn.execute(
        "INSERT INTO events(ts,kind,message) VALUES(?,?,?)",
        (iso_utc(now_utc()), kind, message),
    )


def next_id(conn) -> int:
    """Atomic id allocation — closes the F-02 read-then-write race.

    The UPSERT takes SQLite's write lock for the rest of our transaction,
    so the follow-up SELECT reads our own increment; a concurrent allocator
    blocks on the UPSERT until we commit (busy_timeout=5s). No two callers
    can ever read the same id. UPSERT is SQLite 3.24+ (no 3.35 RETURNING —
    the kiosk device may ship an older distro SQLite). Values are bound,
    never string-formatted (old code passed a bare string as bindings and
    crashed the moment the counter hit 10).
    """
    conn.execute(
        "INSERT INTO meta(k,v) VALUES('next_id','1') "
        "ON CONFLICT(k) DO UPDATE SET v = CAST(v AS INTEGER) + 1"
    )
    row = conn.execute(
        "SELECT CAST(v AS INTEGER) FROM meta WHERE k='next_id'"
    ).fetchone()
    return row[0]


def get_mode(conn) -> str:
    row = conn.execute("SELECT v FROM meta WHERE k='mode'").fetchone()
    return row["v"] if row else "green"


def set_mode(conn, mode) -> str:
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('mode',?)", (mode,))
    log_event(conn, "mode.set", f"operator mode set to {mode}")
    conn.commit()
    return mode


# ---------------------------------------------------------------------------
# Cadence math
# ---------------------------------------------------------------------------


def cadence_parts(cadence):
    """-> (kind, n) where kind in daily/weekdays/weekly/interval, n = period units."""
    if cadence in ("daily", "weekdays"):
        return cadence, 1
    kind, n = cadence.split(":")
    return kind, int(n.rstrip("h"))


def period_len(h) -> timedelta:
    kind, n = cadence_parts(h["cadence"])
    if kind in ("daily", "weekdays"):
        return timedelta(days=1)
    return timedelta(days=n) if kind == "weekly" else timedelta(hours=n)


def period_start(h, dt: datetime) -> datetime:
    """Start of the cadence period containing local datetime dt."""
    kind, n = cadence_parts(h["cadence"])
    if kind in ("daily", "weekdays"):
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    created = local(h["created"])
    if kind == "weekly":
        anchor = created.replace(hour=0, minute=0, second=0, microsecond=0)
        idx = (dt.replace(hour=0, minute=0, second=0, microsecond=0) - anchor).days // n
        return anchor + timedelta(days=idx * n)
    anchor = created.replace(minute=0, second=0, microsecond=0)
    idx = (dt - anchor) // timedelta(hours=n)  # floor, not int(): negatives
    return anchor + idx * timedelta(hours=n)


def is_due_period(h, pstart: datetime) -> bool:
    kind, _ = cadence_parts(h["cadence"])
    if kind == "weekdays":
        return pstart.weekday() < 5  # Mon-Fri; Sat/Sun are grace days
    return True


def has_checkin(conn, habit_id, pstart: datetime) -> bool:
    e = pstart + period_len(_habit_row(conn, habit_id))
    row = conn.execute(
        "SELECT 1 FROM checkins WHERE habit_id=? AND ts>=? AND ts<? LIMIT 1",
        (habit_id, iso_utc(pstart), iso_utc(e)),
    ).fetchone()
    return row is not None


def _habit_row(conn, habit_id):
    return conn.execute("SELECT * FROM habits WHERE id=?", (habit_id,)).fetchone()


def iter_periods(h, start: datetime, end: datetime):
    """Aligned period starts in [start, end)."""
    created_p = period_start(h, local(h["created"]))
    p = period_start(h, start)
    p = max(p, created_p)
    while p < end:
        yield p
        p += period_len(h)


def streak(conn, h, at: datetime | None = None) -> int:
    """Consecutive met due-periods ending now. Current unchecked period is
    grace (never breaks a streak until the period ends)."""
    at = at or datetime.now().astimezone()
    s = period_start(h, at)
    count = 0
    if is_due_period(h, s) and has_checkin(conn, h["id"], s):
        count += 1
    p = s
    while True:
        p -= period_len(h)
        if is_due_period(h, p):
            if not has_checkin(conn, h["id"], p):
                break
            count += 1
    return count


def effective_due(conn, h, at: datetime | None = None) -> bool:
    """Due now, honoring operator mode (deferred effort tiers are not due)."""
    at = at or datetime.now().astimezone()
    if h["effort"] in DEFERRED[get_mode(conn)]:
        return False
    if not is_due_period(h, period_start(h, at)):
        return False
    return not has_checkin(conn, h["id"], period_start(h, at))


# ---------------------------------------------------------------------------
# Habit operations
# ---------------------------------------------------------------------------


def add_habit(
    conn,
    name,
    cadence="daily",
    effort="green",
    category="",
    description="",
    source="human",
    created=None,
):
    if not CADENCE_RE.match(cadence):
        raise ValueError(
            f"bad cadence '{cadence}' (daily|weekdays|weekly:N|interval:Nh)"
        )
    if effort not in EFFORTS:
        raise ValueError(f"bad effort '{effort}' (green|yellow|red)")
    hid = next_id(conn)
    conn.execute(
        "INSERT INTO habits(id,name,description,cadence,effort,category,created)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            hid,
            name,
            description,
            cadence,
            effort,
            category,
            iso_utc(parse_ts(created) if created else now_utc()),
        ),
    )
    log_event(
        conn,
        "habit.add",
        f"#{hid} '{name}' cadence={cadence} effort={effort} by {source}",
    )
    conn.commit()
    return hid


def checkin(conn, habit_id, note="", source="human", at=None, force=False):
    h = _habit_row(conn, habit_id)
    if not h:
        raise ValueError(f"no habit #{habit_id}")
    if h["archived"]:
        raise ValueError(f"habit #{habit_id} is archived")
    ts = iso_utc(parse_ts(at) if at else now_utc())
    p = period_start(h, local(ts))
    if not force and has_checkin(conn, habit_id, p):
        raise ValueError("already checked in this period (use --force)")
    conn.execute(
        "INSERT INTO checkins(habit_id,ts,source,note) VALUES(?,?,?,?)",
        (habit_id, ts, source, note),
    )
    log_event(conn, "habit.check", f"#{habit_id} '{h['name']}' checked in by {source}")
    conn.commit()
    return streak(conn, h, local(ts))


def uncheck(conn, habit_id, at=None):
    h = _habit_row(conn, habit_id)
    if not h:
        raise ValueError(f"no habit #{habit_id}")
    if at:
        p = period_start(h, parse_ts(at))
        n = conn.execute(
            "DELETE FROM checkins WHERE habit_id=? AND ts>=? AND ts<?",
            (habit_id, iso_utc(p), iso_utc(p + period_len(h))),
        ).rowcount
    else:
        row = conn.execute(
            "SELECT ts FROM checkins WHERE habit_id=? ORDER BY ts DESC LIMIT 1",
            (habit_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"no checkins for #{habit_id}")
        n = conn.execute(
            "DELETE FROM checkins WHERE habit_id=? AND ts=?", (habit_id, row["ts"])
        ).rowcount
    log_event(conn, "habit.uncheck", f"#{habit_id} removed {n} checkin(s)")
    conn.commit()
    return n


def edit_habit(
    conn,
    habit_id,
    *,
    name=None,
    description=None,
    cadence=None,
    effort=None,
    category=None,
    archived=None,
):
    h = _habit_row(conn, habit_id)
    if not h:
        raise ValueError(f"no habit #{habit_id}")
    updates = {}
    if name is not None:
        updates["name"] = name
    if description is not None:
        updates["description"] = description
    if cadence is not None:
        if not CADENCE_RE.match(cadence):
            raise ValueError(f"bad cadence '{cadence}'")
        updates["cadence"] = cadence
    if effort is not None:
        if effort not in EFFORTS:
            raise ValueError(f"bad effort '{effort}'")
        updates["effort"] = effort
    if category is not None:
        updates["category"] = category
    if archived is not None:
        updates["archived"] = 1 if archived else 0
    for col, val in updates.items():
        conn.execute(f"UPDATE habits SET {col}=? WHERE id=?", (val, habit_id))
    if updates:
        log_event(conn, "habit.edit", f"#{habit_id} updated {sorted(updates)}")
        conn.commit()
    return updates


# ---------------------------------------------------------------------------
# Views / reports
# ---------------------------------------------------------------------------


def habit_summary(conn, h, at=None):
    at = at or datetime.now().astimezone()
    return {
        "id": h["id"],
        "name": h["name"],
        "cadence": h["cadence"],
        "effort": h["effort"],
        "category": h["category"],
        "archived": bool(h["archived"]),
        "streak": streak(conn, h, at),
        "due": effective_due(conn, h, at),
        "deferred": h["effort"] in DEFERRED[get_mode(conn)],
    }


def due_list(conn, at=None):
    at = at or datetime.now().astimezone()
    return [
        habit_summary(conn, h, at)
        for h in active_habits(conn)
        if effective_due(conn, h, at)
    ]


def active_habits(conn):
    return conn.execute("SELECT * FROM habits WHERE archived=0 ORDER BY id").fetchall()


def report(conn, days=7, at=None):
    """Completion rate per active habit over the trailing window."""
    at = at or datetime.now().astimezone()
    start = at.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days - 1
    )
    out = []
    for h in active_habits(conn):
        due = done = 0
        for p in iter_periods(h, start, at):
            if is_due_period(h, p):
                due += 1
                if has_checkin(conn, h["id"], p):
                    done += 1
        out.append(
            {
                "id": h["id"],
                "name": h["name"],
                "due_periods": due,
                "completed": done,
                "rate": round(done / due, 2) if due else None,
            }
        )
    return out


def log_reading(conn, key, value, unit=""):
    conn.execute(
        "INSERT INTO readings(key,ts,value,unit) VALUES(?,?,?,?)",
        (key, iso_utc(now_utc()), float(value), unit),
    )
    log_event(conn, "reading.log", f"{key}={value}{unit}")
    conn.commit()


def readings(conn, key=None, last=20):
    q = "SELECT * FROM readings"
    args = []
    if key:
        q += " WHERE key=?"
        args.append(key)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(last)
    return [dict(r) for r in conn.execute(q, args)]


def telemetry_stats(conn, hours=24):
    since = iso_utc(now_utc() - timedelta(hours=hours))
    out = {}
    for row in conn.execute(
        "SELECT key, COUNT(*) n, MIN(value) mn, AVG(value) av, MAX(value) mx,"
        " MAX(ts) last_ts, MIN(unit) unit,"
        " (SELECT value FROM readings r2 WHERE r2.key=readings.key"
        "  ORDER BY ts DESC, rowid DESC LIMIT 1) lv"
        " FROM readings WHERE ts>=? GROUP BY key",
        (since,),
    ):
        out[row["key"]] = {
            "count": row["n"],
            "min": round(row["mn"], 3),
            "avg": round(row["av"], 3),
            "max": round(row["mx"], 3),
            "last_value": round(row["lv"], 3) if row["lv"] is not None else None,
            "unit": row["unit"],
            "last": row["last_ts"],
        }
    return out


def post_chat(conn, text, sender="human"):
    """Chat mailbox: human writes from the GUI, agent bot replies via CLI.

    The sender label is the GUI's trust boundary (human vs agent bubble);
    callers cannot pick arbitrary strings. Enforced here — every writer
    routes through this one function (Mantis F-04).
    """
    if sender != "human" and not sender.startswith("agent:"):
        raise ValueError(f"invalid sender {sender!r}: use 'human' or 'agent:<name>'")
    cid = next_id(conn)
    conn.execute(
        "INSERT INTO chat(id,ts,sender,text) VALUES(?,?,?,?)",
        (cid, iso_utc(now_utc()), sender, text),
    )
    log_event(conn, "chat.msg", f"{sender}: {text[:80]}")
    conn.commit()
    return cid


def list_chat(conn, last=50):
    """Oldest→newest so chat views can append; call sites reverse as needed."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM (SELECT * FROM chat ORDER BY id DESC LIMIT ?)"
            " ORDER BY id ASC",
            (last,),
        )
    ]


def deferred_list(conn, at=None):
    """Habits currently due-but-deferred by the operator mode."""
    at = at or datetime.now().astimezone()
    return [
        habit_summary(conn, h, at)
        for h in active_habits(conn)
        if h["effort"] in DEFERRED[get_mode(conn)]
        and is_due_period(h, period_start(h, at))
    ]


# ---------------------------------------------------------------------------
# SOS: program → Hermes instance escalation channel
# ---------------------------------------------------------------------------


def sos_scan(conn, at=None):
    """Detect trouble and file deduped SOS records. Returns new SOS rows.

    Rules (business health, not just crashes):
      - missed_daily       any daily habit overdue by >24h
      - streak_broken      streak hit 0 after having been >=3 (regression)
      - telemetry_stale    no reading for any known key in 48h
      - db_health          the DB itself unreachable (caller reports this)
    Dedup: one pending row per (code) fingerprint — a crash loop files once,
    not once per tick.
    """
    at = at or datetime.now().astimezone()
    findings = []

    for h in active_habits(conn):
        kind, _ = cadence_parts(h["cadence"])
        if kind != "daily":
            continue
        p = period_start(h, at)
        prev = p - period_len(h)
        if local(h["created"]) > prev:
            continue  # habit didn't exist for the missed period — not a miss
        if (
            is_due_period(h, p)
            and not has_checkin(conn, h["id"], p)
            # overdue: previous daily period also went unchecked
            and is_due_period(h, prev)
            and not has_checkin(conn, h["id"], prev)
        ):
            findings.append(
                (
                    "high",
                    "missed_daily",
                    f"'{h['name']}' missed yesterday and still unchecked today",
                    f"habit:{h['id']}",
                )
            )

    for h in active_habits(conn):
        s = streak(conn, h, at)
        if s == 0:
            row = conn.execute(
                "SELECT COUNT(*) n FROM checkins WHERE habit_id=?", (h["id"],)
            ).fetchone()
            if row["n"] >= 3:  # had momentum, now nothing — regression
                findings.append(
                    (
                        "medium",
                        "streak_broken",
                        (
                            f"'{h['name']}' lost a streak of >=3 "
                            f"({row['n']} lifetime check-ins)"
                        ),
                        f"habit:{h['id']}",
                    )
                )

    keys = [r["key"] for r in conn.execute("SELECT DISTINCT key FROM readings")]
    for k in keys:
        row = conn.execute(
            "SELECT MAX(ts) last FROM readings WHERE key=?", (k,)
        ).fetchone()
        if row["last"] and local(row["last"]) < at - timedelta(hours=48):
            findings.append(
                (
                    "medium",
                    "telemetry_stale",
                    f"telemetry '{k}' silent >48h (last {row['last']})",
                    f"key:{k}",
                )
            )

    filed = []
    for severity, code, message, subject in findings:
        # F-01: flatten control-flow — a hostile habit name carrying
        # newlines must not forge transcript boundaries downstream
        message = " ".join(message.split())
        fp = f"{code}:{subject}"
        try:
            conn.execute(
                "INSERT INTO sos(ts,severity,code,message,fingerprint)"
                " VALUES(?,?,?,?,?)",
                (iso_utc(now_utc()), severity, code, message, fp),
            )
            filed.append((severity, code, message))
            log_event(conn, "sos.file", f"[{severity}] {message}")
        except sqlite3.IntegrityError:
            pass  # pending duplicate — already escalated
    if filed:
        conn.commit()
    return filed


def sos_pending(conn):
    return [
        dict(r)
        for r in conn.execute("SELECT * FROM sos WHERE status='pending' ORDER BY id")
    ]


def sos_ack(conn, sos_id, by="agent:hermes-ops", note=""):
    """Agent marks an SOS handled — with an audit trail."""
    row = conn.execute("SELECT * FROM sos WHERE id=?", (sos_id,)).fetchone()
    if not row:
        return f"no SOS #{sos_id}"
    if row["status"] != "pending":
        return f"SOS #{sos_id} already {row['status']}"
    conn.execute(
        "UPDATE sos SET status='acked', ack_ts=?, ack_by=? WHERE id=?",
        (iso_utc(now_utc()), by, sos_id),
    )
    log_event(
        conn,
        "sos.ack",
        f"SOS #{sos_id} ({row['code']}) acked by {by}" + (f" — {note}" if note else ""),
    )
    conn.commit()
    return f"SOS #{sos_id} acknowledged by {by}"


def recent_events(conn, n=100):
    return [
        dict(r)
        for r in conn.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (n,))
    ]


# ---------------------------------------------------------------------------
# Build stamp + release (update) channel
# ---------------------------------------------------------------------------


def build_stamp() -> dict:
    """Version + git hash of the running build, for the GUI header.

    Pure-stdlib discovery: prefers env stamp (CI), then git via subprocess,
    then falls back to '-' so a GUI never fails to load on the device.
    """
    h = os.environ.get("AWSO_BUILD_HASH", "")
    if not h:
        try:
            out = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(Path(__file__).parent),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                h = out.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            h = ""
    return {"version": VERSION, "hash": h or "-", "dirty": git_dirty()}


def git_dirty() -> bool:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(Path(__file__).parent),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return bool(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def sync_releases(conn) -> int:
    """Ingest git history into the releases table (the update channel).

    Each commit becomes a release row: id=hash, version tags via
    AWSO_VERSION env or 'latest', subject=first line, body=rest. Idempotent
    (INSERT OR REPLACE keyed on hash). Returns the number of new rows.
    """
    try:
        out = subprocess.run(
            ["git", "log", "--pretty=%H%x1f%h%x1f%ci%x1f%an%x1f%s%x1f%b%x1e"],
            cwd=str(Path(__file__).parent),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if out.returncode != 0:
            return 0
    except (OSError, subprocess.TimeoutExpired):
        return 0
    new = 0
    for rec in out.stdout.split("\x1e"):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        parts = rec.split("\x1f")
        if len(parts) != 6:
            continue
        full, _short, ts, author, subject, body = [p.strip() for p in parts]
        cur = conn.execute("SELECT 1 FROM releases WHERE id=?", (full,)).fetchone()
        if not cur:
            new += 1
        conn.execute(
            "INSERT OR REPLACE INTO releases(id,ts,version,subject,body,author,files)"
            " VALUES(?,?,?,?,?,?,0)",
            (full, ts, VERSION, subject, body.strip(), author),
        )
    conn.commit()
    return new


def list_releases(conn) -> list[dict]:
    """Newest-first release rows + viewed flag from meta (per operator)."""
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, ts, version, subject, body, author FROM releases"
            " ORDER BY ts DESC LIMIT 50"
        )
    ]
    row = conn.execute("SELECT v FROM meta WHERE k='release_last_viewed'").fetchone()
    last = row["v"] if row else ""
    for r in rows:
        r["hash"] = r["id"][:8]
    # new = appears above the last-viewed entry (nothing viewed -> none new)
    if last:
        seen = False
        for r in rows:
            if r["id"] == last:
                seen = True
            r["new"] = not seen
    else:
        for r in rows:
            r["new"] = False
    return rows


def mark_release_viewed(conn, release_id):
    conn.execute(
        "INSERT OR REPLACE INTO meta(k,v) VALUES('release_last_viewed',?)",
        (release_id,),
    )
    conn.commit()


def habit_detail(conn, habit_id) -> dict:
    """Habit + its whole audit trail: checkins (with source/note), streaks.

    This is what a task row click opens: the 'subagent routines' view — who
    (human vs agent:awso-agentd) did what, when, with what note.
    """
    h = _habit_row(conn, habit_id)
    if not h:
        raise ValueError(f"no habit #{habit_id}")
    checkins = [
        dict(r)
        for r in conn.execute(
            "SELECT ts, source, note FROM checkins WHERE habit_id=? ORDER BY ts DESC",
            (habit_id,),
        )
    ]
    events = [
        dict(r)
        for r in conn.execute(
            "SELECT ts, kind, message FROM events"
            " WHERE message LIKE ? ORDER BY ts DESC LIMIT 30",
            (f"%#{habit_id}%",),
        )
    ]
    s = habit_summary(conn, h)
    s["description"] = h["description"]
    s["checkins"] = checkins
    s["events"] = events
    return s


# ---------------------------------------------------------------------------
# Fleet: the agent/model/skill inventory the GUI's hamburger panel shows.
# Reads the operator's Hermes profile ($HERMES_HOME, default ~/.hermes) and
# the live process table — no network, no spawned shells at rest. Every
# probe is a stat() or a PATH walk: the panel must stay sub-100ms on the
# solar device, so anything slower (model catalogs, ollama probes) is
# opt-in via `fleet probe`.
# ---------------------------------------------------------------------------

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

# Delegate CLIs the panel probes with shutil.which — name -> display label.
# Sourced from the installed *-delegate skills (autonomous-ai-agents/).
FLEET_AGENT_CLIS = (
    ("claude", "Claude Code"),
    ("codex", "Codex"),
    ("cursor-agent", "Cursor"),
    ("aider", "Aider"),
    ("opencode", "OpenCode"),
    ("gh copilot", "Copilot"),  # gh extension form on the PATH
    ("gh", "Copilot CLI"),
    ("gemini", "Gemini CLI"),
    ("kimi", "Kimi"),
    ("grok", "Grok"),
    ("warp", "Warp"),
    ("qoder", "Qoder"),
    ("vibe", "Vibe"),
    ("zcode", "Zcode"),
    ("cline", "Cline"),
    ("pi", "pi"),
    ("agy", "agy"),
    ("omp", "omp"),
    ("commandcode", "CommandCode"),
)


def _skill_meta(skill_dir: Path):
    """Parse a SKILL.md's frontmatter without yaml: name, desc, author, source."""
    out = {"name": skill_dir.name, "description": "", "author": "", "version": ""}
    try:
        first = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", first, re.DOTALL)
    if not m:
        return out
    for line in m.group(1).splitlines():
        kv = re.match(r"^(name|description|author|version):\s*(.*)", line)
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2).strip().strip("\"'")
        if val and val not in (">-", "|", ">"):
            out[key] = val[:200]
    return out


# Small, LOCAL, tool-call-capable models only — the fleet's model policy
# (operator ruling): anything the system delegates to must be able to run
# on the device itself and drive habitctl via tool calls. Sorted ascending
# by footprint; ram_gb is the practical all-in budget (weights + KV cache +
# runtime, q4_K_M). No cloud models are ever recommended; cloud appears in
# the panel only as a labeled "online-only" tier for the operator to see.
FLEET_LOCAL_MODELS = (
    # name, params(B), ram_gb, tool_call, note
    ("qwen3:1.7b-awso-q4km", 1.7, 1.8, True, "AWSO-trained: MoD + Neuralese, q4_K_M"),
    ("qwen3:1.7b", 1.7, 3.0, True, "smallest reliable tool-caller"),
    ("llama3.2:3b", 3.2, 4.0, True, "Meta small; decent tool use"),
    ("qwen3:4b", 4.0, 5.0, True, "best quality/size for tools"),
    ("granite3.3:2b", 2.5, 3.5, True, "IBM; tight tool schemas"),
    ("qwen3:8b", 8.0, 9.0, True, "when RAM is plentiful"),
)


def hardware_profile() -> dict:
    """Cheap hardware snapshot for local-model selection (pure stdlib, cached).

    RAM via /proc/meminfo (Linux) or ctypes GlobalMemoryStatusEx (Windows);
    GPU presence is best-effort — a solar kiosk usually has none, and the
    software renderer already assumes that.
    """
    ram_gb = None
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            ram_gb = round(stat.ullTotalPhys / (1024**3), 1)
        else:
            with open("/proc/meminfo", encoding="ascii") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        ram_gb = round(int(line.split()[1]) / (1024**2), 1)
                        break
    except Exception:  # noqa: S110, BLE001 — hardware probing is best-effort
        pass
    gpu = (
        bool(list(Path("/dev/dri").glob("card*")))
        if sys.platform.startswith("linux")
        else False
    )
    return {
        "ram_gb": ram_gb,
        "cpus": os.cpu_count(),
        "gpu": gpu,
        "platform": sys.platform,
    }


def recommend_local_model(hw: dict) -> dict:
    """Smallest tool-capable local model that fits the hardware, per policy.

    Budget = half of RAM (GUI + SQLite + agentd live alongside). If RAM is
    unknown, be conservative: assume 4 GB.
    """
    ram = hw.get("ram_gb") or 4.0
    budget = max(ram * 0.5, 1.5)
    for name, params, ram_gb, tools, note in FLEET_LOCAL_MODELS:
        if tools and ram_gb <= budget:
            return {
                "name": name,
                "params_b": params,
                "ram_gb": ram_gb,
                "tool_call": True,
                "note": note,
                "ram_budget_gb": round(budget, 1),
            }
    # nothing fits: smallest tool-caller anyway, flagged over-budget
    name, params, ram_gb, tools, note = FLEET_LOCAL_MODELS[0]
    return {
        "name": name,
        "params_b": params,
        "ram_gb": ram_gb,
        "tool_call": True,
        "note": note + " (over RAM budget — expect swapping)",
        "ram_budget_gb": round(budget, 1),
        "over_budget": True,
    }


def fleet_scan(probe: bool = False) -> dict:
    """Inventory agents/skills/models for the fleet panel. Pure-stdlib, fast.

    Model policy (operator ruling): the fleet recommends only small, LOCAL,
    tool-call-capable models sized to the actual hardware. Cloud models are
    shown as an online-only tier, never recommended. `probe=True` adds the
    one ~50-100ms ollama REST call (panel Refresh button / CLI only — never
    the 15s refresh cycle).
    """
    import shutil

    home = HERMES_HOME
    skills_dir = home / "skills"

    # -- agents: delegate CLIs (which()) --------------------------------------
    agents = []
    for exe, label in FLEET_AGENT_CLIS:
        # "gh copilot" probes `gh`; the extension form needs the gh binary.
        found = shutil.which(exe.split()[0])
        if found:
            agents.append(
                {
                    "name": label,
                    "kind": "delegate-cli",
                    "online": True,
                    "detail": found,
                }
            )
    agents.sort(key=lambda a: a["name"].lower())

    # -- skills: walk the profile's skills tree ------------------------------
    skills = []
    if skills_dir.is_dir():
        for f in sorted(skills_dir.glob("*/*/SKILL.md")):
            meta = _skill_meta(f.parent)
            meta["category"] = f.parent.parent.name
            skills.append(meta)
        # flat-layout skills (obra/superpowers origin) at skills/<name>/
        for f in sorted(skills_dir.glob("*/SKILL.md")):
            if f.parent.parent == skills_dir:
                meta = _skill_meta(f.parent)
                meta["category"] = ""
                skills.append(meta)

    # -- models: local-first policy -------------------------------------------
    hw = hardware_profile()
    recommended = recommend_local_model(hw)
    local_models = []
    ollama_uri = "http://127.0.0.1:11434"
    if probe:
        import urllib.request

        try:
            with urllib.request.urlopen(ollama_uri + "/api/tags", timeout=2) as r:
                installed = {
                    m_.get("name", "?"): m_ for m_ in json.load(r).get("models", [])
                }
            for name, params, ram_gb, tools, note in FLEET_LOCAL_MODELS:
                if name in installed:
                    local_models.append(
                        {
                            "name": name,
                            "kind": "local",
                            "online": True,
                            "tool_call": tools,
                            "detail": "installed via ollama",
                        }
                    )
            recommended["installed"] = recommended["name"] in installed
        except Exception:  # noqa: BLE001 — ollama absent is the normal case
            local_models = []

    # cloud tier: display-only, never recommended (policy: local-first)
    cloud = []
    conf = home / "config.yaml"
    if conf.is_file():
        try:
            text = conf.read_text(encoding="utf-8", errors="replace")
            mm = re.search(
                r"^model:\s*\n(?:^  .*\n)*?^  default:\s*(\S+)", text, re.MULTILINE
            )
            if mm:
                entry = {
                    "name": mm.group(1),
                    "kind": "cloud",
                    "online": None,
                    "detail": "online-only tier — not recommended for delegation",
                }
                prov = re.search(r"^  provider:\s*(\S+)", text, re.MULTILINE)
                if prov:
                    entry["detail"] += f" (provider {prov.group(1)})"
                cloud.append(entry)
        except OSError:
            pass

    return {
        "hermes_home": str(home),
        "agents": agents,
        "skills": skills,
        "models": local_models + cloud,
        "hardware": hw,
        "recommended_model": recommended,
        "model_policy": "small + local + tool-call-capable only",
        "counts": {
            "agents": len(agents),
            "skills": len(skills),
            "models": len(local_models) + len(cloud),
        },
        "credits": fleet_credits(),
    }


FLEET_CREDITS = (
    # label, upstream repo README (source-of-truth for credit + license)
    ("Hermes Agent", "https://github.com/NousResearch/hermes-agent"),
    (
        "superpowers — subagent-driven-development (Jesse Vincent)",
        "https://github.com/obra/superpowers",
    ),
    (
        "super-hermes — prism analysis skills (Cranot)",
        "https://github.com/Cranot/super-hermes",
    ),
    ("Mantis security review (google/mantis)", "https://github.com/google/mantis"),
    ("Sunbeam/Ionic design tokens", "https://ionicframework.com/docs"),
)


def fleet_credits() -> list:
    """Upstream README URLs for the panel's Credits section."""
    return [{"label": label, "url": url} for label, url in FLEET_CREDITS]


# ---------------------------------------------------------------------------
# Headless services: display detection + watch loop
# ---------------------------------------------------------------------------


def display_present() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False  # remote session, no local seat
    return bool(list(Path("/dev/dri").glob("card*")))  # real seat with a GPU


def spawn_gui():
    gui = Path(__file__).with_name("habit_gui.py")
    if not gui.exists():
        raise FileNotFoundError("habit_gui.py not found next to habit_core.py")
    log_event(connect(), "gui.spawn", "GUI requested")  # separate conn, cheap
    # ponytail: GUI respawn tracking lives in the watch loop's Popen handle,
    # not a lockfile — watch only manages GUIs it spawned.
    return subprocess.Popen(
        [sys.executable, str(gui)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # detach: survives watch restarts
    )


def watch(db=None, interval=60, headless=False, write=print):
    conn = connect(db)
    log_event(
        conn, "watch.start", f"watch loop (interval={interval}s, headless={headless})"
    )
    conn.commit()
    gui_proc = None
    write(
        f"watch: ticking every {interval}s "
        f"({'headless' if headless else 'GUI attach on display'})"
    )
    while True:
        try:
            if not headless and gui_proc is None and display_present():
                gui_proc = spawn_gui()
                write(f"watch: spawned GUI (pid {gui_proc.pid})")
            if gui_proc is not None and gui_proc.poll() is not None:
                write(
                    f"watch: GUI exited (code {gui_proc.returncode}); "
                    f"will respawn next tick if a display is present"
                )
                gui_proc = None
            sos_scan(conn)  # health rules run every tick
        except sqlite3.Error as e:
            # never die on a transient DB hiccup; DB-down itself is SOS-worthy
            # but this process IS the monitor — log to stdout, keep ticking
            write(f"watch: db error during tick (continuing): {e}")
        time.sleep(interval)


# ---------------------------------------------------------------------------
# CLI (also the subagent control surface)
# ---------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(prog="habitctl", description="AWSO_BSPT habit tracker")
    p.add_argument(
        "--db",
        help=f"database path (default $AWSO_DB or "
        f"{default_db().name} beside the script)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_json(sp):
        sp.add_argument("--json", action="store_true", help="machine output")
        return sp

    sp = sub.add_parser("add", help="create a habit")
    sp.add_argument("name")
    sp.add_argument(
        "--cadence", default="daily", help="daily|weekdays|weekly:N|interval:Nh"
    )
    sp.add_argument("--effort", default="green", help="green|yellow|red")
    sp.add_argument("--category", default="")
    sp.add_argument("--description", default="")
    sp.add_argument("--source", default="human")
    sp.add_argument("--created", help="backdate creation (ISO) — for imports")

    sp = sub.add_parser("list", help="list habits")
    sp.add_argument("--all", action="store_true", help="include archived")
    with_json(sp)

    sp = sub.add_parser("check", help="check in a habit")
    sp.add_argument("id", type=int)
    sp.add_argument("--note", default="")
    sp.add_argument("--source", default="human")
    sp.add_argument("--at", help="backfill timestamp (ISO, local if naive)")
    sp.add_argument("--force", action="store_true")

    sp = sub.add_parser("uncheck", help="remove latest (or --at period) checkin")
    sp.add_argument("id", type=int)
    sp.add_argument("--at")

    sp = sub.add_parser("info", help="habit detail")
    sp.add_argument("id", type=int)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("edit", help="modify a habit")
    sp.add_argument("id", type=int)
    sp.add_argument("--name")
    sp.add_argument("--description")
    sp.add_argument("--cadence")
    sp.add_argument("--effort")
    sp.add_argument("--category")
    sp.add_argument("--archive", action="store_true")
    sp.add_argument("--unarchive", action="store_true")

    sp = sub.add_parser("due", help="what's due now (mode-aware)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("streaks", help="streak table")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("mode", help="get/set operator capacity mode")
    sp.add_argument("value", choices=MODES, nargs="?")

    sp = sub.add_parser("report", help="completion rates")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("status", help="one-glance overview")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("log-reading", help="record telemetry point")
    sp.add_argument("key")
    sp.add_argument("value", type=float)
    sp.add_argument("unit", nargs="?")

    sp = sub.add_parser("readings", help="recent telemetry")
    sp.add_argument("--key")
    sp.add_argument("--last", type=int, default=20)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("stats", help="telemetry stats (default last 24h)")
    sp.add_argument("--hours", type=int, default=24)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("chat", help="agent chat mailbox (send/list)")
    sub_chat = sp.add_subparsers(dest="chat_cmd", required=True)
    sp_c = sub_chat.add_parser("send", help="post a message")
    sp_c.add_argument("text")
    sp_c.add_argument("--sender", default="agent:hermes-ops")
    sp_c2 = sub_chat.add_parser("list", help="recent messages")
    sp_c2.add_argument("--last", type=int, default=50)
    sp_c2.add_argument("--json", action="store_true")

    sp = sub.add_parser("sos", help="SOS escalation channel (scan/list/ack)")
    sub_sos = sp.add_subparsers(dest="sos_cmd", required=True)
    sp_s = sub_sos.add_parser("scan", help="run health rules now")
    with_json(sp_s)
    sp_s2 = sub_sos.add_parser("list", help="pending SOS (default all recent)")
    sp_s2.add_argument("--all", action="store_true")
    with_json(sp_s2)
    sp_s3 = sub_sos.add_parser("ack", help="acknowledge an SOS")
    sp_s3.add_argument("id", type=int)
    sp_s3.add_argument("--by", default="agent:hermes-ops")
    sp_s3.add_argument("--note", default="")

    sp = sub.add_parser("events", help="recent audit events")
    sp.add_argument("--last", type=int, default=50)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("build", help="build stamp: version + git hash")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("releases", help="update channel: sync git history, list, view")
    sub_rel = sp.add_subparsers(dest="rel_cmd", required=True)
    sp_r = sub_rel.add_parser("sync", help="ingest git log into releases")
    sp_r.add_argument("--json", action="store_true")
    sp_r2 = sub_rel.add_parser("list", help="list known releases")
    with_json(sp_r2)
    sp_r3 = sub_rel.add_parser("view", help="show one release; marks it viewed")
    sp_r3.add_argument("id")

    sp = sub.add_parser("fleet", help="agent/model/skill fleet inventory (GUI panel)")
    sp.add_argument("--json", action="store_true", help="machine output")
    sp.add_argument("--probe", action="store_true", help="include ollama REST probe")

    sp = sub.add_parser("model", help="local model management (train/export/bench)")
    sub_model = sp.add_subparsers(dest="model_cmd", required=True)
    sp_m = sub_model.add_parser("prepare", help="extract training pairs from DB")
    sp_m.add_argument("--db", default=None)
    sp_m.add_argument("--out", default="data/training_pairs.jsonl")
    sp_m.add_argument("--limit", type=int, default=2000)
    sp_m = sub_model.add_parser("train", help="QLoRA fine-tune base model")
    sp_m.add_argument("--base", default="qwen3:1.7b")
    sp_m.add_argument("--data", default="data/training_pairs.jsonl")
    sp_m.add_argument("--out", default="models/qwen3-1.7b-awso-lora")
    sp_m.add_argument("--epochs", type=int, default=3)
    sp_m.add_argument("--batch-size", type=int, default=1)
    sp_m.add_argument("--lr", type=float, default=2e-4)
    sp_m.add_argument("--lora-r", type=int, default=16)
    sp_m.add_argument("--lora-alpha", type=int, default=32)
    sp_m.add_argument("--no-qlora", action="store_true")
    sp_m = sub_model.add_parser("router", help="train neuralese routers + depth gates")
    sp_m.add_argument("--base", default="qwen3:1.7b")
    sp_m.add_argument("--lora", default="models/qwen3-1.7b-awso-lora")
    sp_m.add_argument("--out", default="models/qwen3-1.7b-awso-router")
    sp_m.add_argument("--epochs", type=int, default=2)
    sp_m = sub_model.add_parser("export", help="merge + convert to GGUF")
    sp_m.add_argument("--base", default="qwen3:1.7b")
    sp_m.add_argument("--lora", default="models/qwen3-1.7b-awso-lora")
    sp_m.add_argument("--router", default="models/qwen3-1.7b-awso-router")
    sp_m.add_argument("--out", default="models/qwen3-1.7b-awso-q4km.gguf")
    sp_m.add_argument("--quant", default="q4_K_M")
    sp_m = sub_model.add_parser("bench", help="benchmark GGUF model")
    sp_m.add_argument("--model", required=True)
    sp_m.add_argument("--prompt", default="list habits due today")
    sp_m.add_argument("--tokens", type=int, default=128)
    sp_m.add_argument("--threads", type=int, default=None)
    sp_m = sub_model.add_parser("pipeline", help="run full pipeline")
    sp_m.add_argument("--db", default=None)
    sp_m.add_argument("--base", default="qwen3:1.7b")
    sp_m.add_argument("--out-dir", default="models")

    sp = sub.add_parser("watch", help="daemon: attach GUI when display appears")
    sp.add_argument("--interval", type=int, default=60)
    sp.add_argument("--headless", action="store_true", help="never spawn the GUI")

    sp = sub.add_parser("gui", help="launch the GUI now (needs a display)")
    sp = sub.add_parser("selftest")
    sp = sub.add_parser("version")
    sp = sub.add_parser("help", help="list available verbs")
    return p


# Verbs the kiosk console may run. `watch` is an unbounded while-True loop
# (freezes the Qt main thread forever — Mantis F-05) and `gui` spawns a
# second GUI process; both are daemon-verbs, not interactive ones.
CONSOLE_SAFE_VERBS = (
    "add",
    "list",
    "check",
    "uncheck",
    "info",
    "edit",
    "due",
    "streaks",
    "mode",
    "report",
    "status",
    "log-reading",
    "readings",
    "stats",
    "chat",
    "sos",
    "events",
    "build",
    "releases",
    "model",
    "selftest",
    "version",
    "help",
)


def help_text() -> str:
    lines = [
        "habitctl verbs:",
        "  status                 one-glance overview",
        "  due [--json]           what's due now (mode-aware)",
        "  list [--all]           list habits",
        "  add NAME [--cadence C] [--effort E] [--category CAT]   create",
        "                         cadence: daily|weekdays|weekly:N|interval:Nh",
        "                         effort: green|yellow|red",
        "  check ID [--note TXT]  check in a habit",
        "  uncheck ID             remove latest checkin",
        "  info ID                habit detail",
        "  edit ID [--name N] ... modify / --archive / --unarchive",
        "  streaks                streak table",
        "  mode [green|yellow|red]  get/set operator capacity mode",
        "  report [--days N]      completion rates",
        "  log-reading KEY V [UNIT]  record telemetry point",
        "  readings [--key K]     recent telemetry",
        "  stats [--hours N]      telemetry stats",
        "  chat send TEXT         post to the agent mailbox",
        "  chat list [--last N]   recent messages",
        "  sos scan|list|ack       escalation channel",
        "  events [--last N]      recent audit events",
        "  build                  build stamp: version + git hash",
        "  releases sync|list|view  update channel (CI changelog)",
        "  model prepare|train|router|export|bench|pipeline  local model mgmt",
        "  selftest | version | help",
        "  (watch/gui are daemon verbs — not available in this console)",
    ]
    return "\n".join(lines)


def cli(argv=None, write=print) -> int:
    args = build_parser().parse_args(argv)
    conn = connect(args.db)
    J = lambda o: write(json.dumps(o, indent=2))

    try:
        if args.cmd == "add":
            hid = add_habit(
                conn,
                args.name,
                args.cadence,
                args.effort,
                args.category,
                args.description,
                args.source,
                args.created,
            )
            write(
                f"added habit #{hid}: {args.name} "
                f"({args.cadence}, effort {args.effort})"
            )

        elif args.cmd == "list":
            rows = (
                conn.execute("SELECT * FROM habits ORDER BY id").fetchall()
                if args.all
                else active_habits(conn)
            )
            habits = [habit_summary(conn, h) for h in rows]
            if args.json:
                J(habits)
            else:
                for s in habits:
                    flag = " (archived)" if s["archived"] else ""
                    mark = (
                        "✓"
                        if not s["due"] and not s["deferred"]
                        else ("⊘ deferred" if s["deferred"] else "•")
                    )
                    write(
                        f"#{s['id']:>3} {mark} {s['name']}{flag} — "
                        f"{s['cadence']}, streak {s['streak']}, "
                        f"effort {s['effort']}"
                    )

        elif args.cmd == "check":
            s = checkin(conn, args.id, args.note, args.source, args.at, args.force)
            write(f"checked in habit #{args.id} (streak now {s})")

        elif args.cmd == "uncheck":
            n = uncheck(conn, args.id, args.at)
            write(f"removed {n} checkin(s) from habit #{args.id}")

        elif args.cmd == "info":
            h = _habit_row(conn, args.id)
            if not h:
                raise ValueError(f"no habit #{args.id}")
            if args.json:
                J(
                    {
                        **habit_summary(conn, h),
                        "description": h["description"],
                        "created": h["created"],
                    }
                )
            else:
                s = habit_summary(conn, h)
                write(
                    f"#{s['id']} {s['name']}\n  cadence:  {s['cadence']}"
                    f"\n  effort:   {s['effort']}  (mode {get_mode(conn)},"
                    f" deferred={s['deferred']})\n  streak:   {s['streak']}"
                    f"\n  due now:  {s['due']}"
                    f"\n  created:  {h['created']}"
                    f"\n  about:    {h['description'] or '—'}"
                )

        elif args.cmd == "edit":
            if args.archive and args.unarchive:
                raise ValueError("pick one of --archive/--unarchive")
            edits = edit_habit(
                conn,
                args.id,
                name=args.name,
                description=args.description,
                cadence=args.cadence,
                effort=args.effort,
                category=args.category,
                archived=True if args.archive else (False if args.unarchive else None),
            )
            write(f"habit #{args.id}: updated {sorted(edits) if edits else 'nothing'}")

        elif args.cmd == "due":
            d = due_list(conn)
            if args.json:
                J(d)
            elif not d:
                write("nothing due — all clear")
            else:
                for s in d:
                    write(
                        f"#{s['id']:>3} • {s['name']} ({s['cadence']}, "
                        f"streak {s['streak']})"
                    )

        elif args.cmd == "streaks":
            rows = [
                {"id": h["id"], "name": h["name"], "streak": streak(conn, h)}
                for h in active_habits(conn)
            ]
            if args.json:
                J(rows)
            else:
                for r in rows:
                    fire = "🔥" if r["streak"] >= 3 else " "
                    write(f"#{r['id']:>3} {fire} {r['name']}: {r['streak']}")

        elif args.cmd == "mode":
            if args.value:
                set_mode(conn, args.value)
            write(f"operator mode: {get_mode(conn)}")

        elif args.cmd == "report":
            rep = report(conn, args.days)
            if args.json:
                J(rep)
            else:
                write(f"completion over last {args.days} days:")
                for r in rep:
                    rate = f"{r['rate'] * 100:.0f}%" if r["rate"] is not None else "—"
                    write(
                        f"  #{r['id']:>3} {r['name'][:28]:<28} "
                        f"{r['completed']}/{r['due_periods']}  {rate}"
                    )

        elif args.cmd == "status":
            mode = get_mode(conn)
            due = due_list(conn)
            deferred = deferred_list(conn)
            st = telemetry_stats(conn)
            latest = {k: v["last"] for k, v in st.items()}
            # agent section: agentd presence, current activity, recent trace
            hb = conn.execute("SELECT v FROM meta WHERE k='agent_heartbeat'").fetchone()
            agent = {"online": False, "age_s": None, "activity": None, "trace": []}
            if hb:
                try:
                    age = (now_utc() - parse_ts(hb["v"])).total_seconds()
                    agent["online"] = age < 90
                    agent["age_s"] = int(age)
                except ValueError:
                    pass
            act = conn.execute("SELECT v FROM meta WHERE k='agent_status'").fetchone()
            if act:
                agent["activity"] = act["v"]
            agent["trace"] = [
                f"{r['ts'][11:19]} {r['kind']} {r['message'][:60]}"
                for r in conn.execute(
                    "SELECT ts, kind, message FROM events"
                    " WHERE kind LIKE 'agentd.%' ORDER BY ts DESC LIMIT 8"
                )
            ]
            payload = {
                "mode": mode,
                "due_count": len(due),
                "deferred_count": len(deferred),
                "due": [d["name"] for d in due],
                "deferred": [d["name"] for d in deferred],
                "telemetry_keys": sorted(st),
                "agent": agent,
                "time": now_utc().isoformat(timespec="seconds"),
            }
            if args.json:
                J(payload)
            else:
                write(f"AWSO_BSPT — {payload['time']}")
                write(
                    f"mode {mode.upper()} | due {len(due)} | deferred {len(deferred)}"
                )
                for d in due:
                    write(f"  due:      {d['name']} (streak {d['streak']})")
                for d in deferred:
                    write(f"  deferred: {d['name']}")
                for k, ts in latest.items():
                    write(f"  reading:  {k} last at {ts}")
                # verbose agent block — the operator's chain-of-thought view
                state = "ONLINE" if agent["online"] else "OFFLINE"
                age = (
                    f" (heartbeat {agent['age_s']}s ago)"
                    if agent["age_s"] is not None
                    else ""
                )
                write(f"  agent:    {state}{age}")
                if agent["activity"]:
                    write(f"  activity: {agent['activity']}")
                for t in agent["trace"]:
                    write(f"    {t}")

        elif args.cmd == "log-reading":
            log_reading(conn, args.key, args.value, args.unit or "")
            write(f"logged {args.key}={args.value}{args.unit or ''}")

        elif args.cmd == "readings":
            rows = readings(conn, args.key, args.last)
            if args.json:
                J(rows)
            else:
                for r in rows:
                    write(f"{r['ts']}  {r['key']}={r['value']}{r['unit']}")

        elif args.cmd == "stats":
            st = telemetry_stats(conn, args.hours)
            if args.json:
                J(st)
            else:
                write(f"telemetry, last {args.hours}h:")
                for k, v in st.items():
                    write(
                        f"  {k}: n={v['count']} min={v['min']} "
                        f"avg={v['avg']} max={v['max']} {v['unit']}"
                    )

        elif args.cmd == "chat":
            if args.chat_cmd == "send":
                cid = post_chat(conn, args.text, args.sender)
                write(f"posted chat #{cid} as {args.sender}")
            else:
                msgs = list_chat(conn, args.last)
                if args.json:
                    J(msgs)
                else:
                    for m in msgs:
                        write(f"{m['ts']} {m['sender']}: {m['text']}")

        elif args.cmd == "sos":
            if args.sos_cmd == "scan":
                filed = sos_scan(conn)
                if args.json:
                    J([{"severity": s, "code": c, "message": m} for s, c, m in filed])
                elif not filed:
                    write("sos scan: all clear")
                else:
                    for s, c, m in filed:
                        write(f"sos filed [{s}] {c}: {m}")
            elif args.sos_cmd == "list":
                if args.all:
                    rows = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT * FROM sos ORDER BY id DESC LIMIT 50"
                        )
                    ]
                else:
                    rows = sos_pending(conn)
                if args.json:
                    J(rows)
                else:
                    for r in rows:
                        write(
                            f"#{r['id']} [{r['severity']}] {r['code']} — "
                            f"{r['message']} ({r['status']})"
                        )
            elif args.sos_cmd == "ack":
                write(sos_ack(conn, args.id, args.by, args.note))

        elif args.cmd == "events":
            ev = recent_events(conn, args.last)
            if args.json:
                J(ev)
            else:
                for e in reversed(ev):
                    write(f"{e['ts']} [{e['kind']}] {e['message']}")

        elif args.cmd == "build":
            st = build_stamp()
            if args.json:
                J(st)
            else:
                dirty = " (dirty tree)" if st["dirty"] else ""
                write(f"habitctl {st['version']}+{st['hash']}{dirty}")

        elif args.cmd == "fleet":
            inv = fleet_scan(probe=args.probe)
            if args.json:
                J(inv)
            else:
                write(f"fleet @ {inv['hermes_home']}")
                write(
                    f"{inv['counts']['agents']} agent CLIs, "
                    f"{inv['counts']['skills']} skills, "
                    f"{inv['counts']['models']} models"
                )
                write("")
                write("agents (delegate CLIs on PATH):")
                if inv["agents"]:
                    for a in inv["agents"]:
                        write(f"  ● {a['name']} — {a['detail']}")
                else:
                    write("  (none installed)")
                write("")
                write("models:")
                for m in inv["models"]:
                    state = (
                        "local"
                        if m.get("kind") == "local"
                        else m.get("provider", "cloud")
                    )
                    write(f"  ● {m['name']} [{state}]")
                write("")
                write(f"skills ({inv['counts']['skills']}):")
                for s in inv["skills"]:
                    cat = f"{s['category']}/" if s["category"] else ""
                    write(f"  ◦ {cat}{s['name']}")
                    if s["description"]:
                        write(f"      {s['description'][:96]}")
                write("")
                write("credits:")
                for c in inv["credits"]:
                    write(f"  {c['label']}")
                    write(f"    {c['url']}")

        elif args.cmd == "releases":
            if args.rel_cmd == "sync":
                n = sync_releases(conn)
                if args.json:
                    J({"new": n})
                else:
                    write(f"releases synced: {n} new")
            elif args.rel_cmd == "list":
                rows = list_releases(conn)
                if args.json:
                    J(rows)
                else:
                    for r in rows:
                        badge = "NEW " if r["new"] else "    "
                        write(
                            f"{badge}{r['hash']}  {r['ts'][:19]}  "
                            f"[{r['version']}] {r['subject']}"
                        )
            elif args.rel_cmd == "view":
                row = conn.execute(
                    "SELECT * FROM releases WHERE id LIKE ?",
                    (args.id + "%",),
                ).fetchone()
                if not row:
                    write(f"error: no release matching '{args.id}'")
                    return 1
                mark_release_viewed(conn, row["id"])
                write(f"{row['id']}")
                write(f"version {row['version']}  {row['ts']}")
                write(f"author {row['author']}")
                write("")
                write(row["subject"])
                if row["body"]:
                    write("")
                    write(row["body"])

        elif args.cmd == "model":
            # Delegate to model_trainer.py (lazy import, ML deps optional)
            try:
                import model_trainer as mt
            except ImportError as e:
                write(f"error: model_trainer unavailable: {e}")
                return 1

            # Build argv for model_trainer
            mt_argv = [args.model_cmd]
            if args.model_cmd == "prepare":
                if args.db:
                    mt_argv += ["--db", args.db]
                if args.out:
                    mt_argv += ["--out", args.out]
                if args.limit:
                    mt_argv += ["--limit", str(args.limit)]
            elif args.model_cmd == "train":
                mt_argv += ["--base", args.base, "--data", args.data, "--out", args.out]
                mt_argv += [
                    "--epochs",
                    str(args.epochs),
                    "--batch-size",
                    str(args.batch_size),
                ]
                mt_argv += [
                    "--lr",
                    str(args.lr),
                    "--lora-r",
                    str(args.lora_r),
                    "--lora-alpha",
                    str(args.lora_alpha),
                ]
                if args.no_qlora:
                    mt_argv += ["--no-qlora"]
            elif args.model_cmd == "router":
                mt_argv += ["--base", args.base, "--lora", args.lora, "--out", args.out]
                mt_argv += ["--epochs", str(args.epochs)]
            elif args.model_cmd == "export":
                mt_argv += [
                    "--base",
                    args.base,
                    "--lora",
                    args.lora,
                    "--router",
                    args.router,
                    "--out",
                    args.out,
                ]
                mt_argv += ["--quant", args.quant]
            elif args.model_cmd == "bench":
                mt_argv += ["--model", args.model, "--prompt", args.prompt]
                mt_argv += ["--tokens", str(args.tokens)]
                if args.threads:
                    mt_argv += ["--threads", str(args.threads)]
            elif args.model_cmd == "pipeline":
                mt_argv += [
                    "--db",
                    args.db or "",
                    "--base",
                    args.base,
                    "--out-dir",
                    args.out_dir,
                ]

            # Filter out None/empty
            mt_argv = [a for a in mt_argv if a]
            try:
                mt.main(mt_argv)
            except SystemExit as e:
                return e.code if isinstance(e.code, int) else 1
            except Exception as e:  # noqa: BLE001 — CLI boundary: any failure -> rc 1
                write(f"error: {e}")
                return 1

        elif args.cmd == "watch":
            watch(args.db, args.interval, args.headless, write)
            return 0

        elif args.cmd == "gui":
            if not display_present():
                write(
                    "no display found — GUI needs a real session "
                    "(or set QT_QPA_PLATFORM=offscreen to test headless)"
                )
                return 1
            proc = spawn_gui()
            write(f"GUI launched (pid {proc.pid})")

        elif args.cmd == "selftest":
            return selftest(write)

        elif args.cmd == "version":
            write(f"habitctl {VERSION}")

        elif args.cmd == "help":
            write(help_text())

        return 0
    except (ValueError, FileNotFoundError) as e:
        write(f"error: {e}")
        return 1


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def selftest(write=print) -> int:
    tmp = tempfile.mktemp(suffix=".db")
    fails = []

    def ok(name, cond):
        write(("  PASS " if cond else "  FAIL ") + name)
        if not cond:
            fails.append(name)

    conn = connect(tmp)
    write(f"selftest on {tmp}")

    # adds + validation (h1 backdated: imports carry history before today)
    now = datetime.now().astimezone()
    h1 = add_habit(
        conn,
        "Morning inventory scan",
        "daily",
        "green",
        created=(now - timedelta(days=10)).isoformat(),
    )
    h2 = add_habit(conn, "Weekly deep clean", "weekly:7", "red", "ops")
    h3 = add_habit(conn, "Standup", "weekdays", "yellow")
    h4 = add_habit(conn, "Battery voltage check", "interval:6h", "green")
    ok("ids assigned sequentially", (h1, h2, h3, h4) == (1, 2, 3, 4))
    try:
        add_habit(conn, "bad", "hourly")
        ok("bad cadence rejected", False)
    except ValueError:
        ok("bad cadence rejected", True)

    # daily streak via backfill
    checkin(conn, h1, at=(now - timedelta(days=2)).isoformat(), source="agent:ci")
    checkin(conn, h1, at=(now - timedelta(days=1)).isoformat())
    ok("streak 2 after two backfilled days", streak(conn, _habit_row(conn, h1)) == 2)
    ok(
        "today is grace — unchecked doesn't break",
        effective_due(conn, _habit_row(conn, h1)) is True,
    )
    checkin(conn, h1)
    ok("streak 3 after today", streak(conn, _habit_row(conn, h1)) == 3)

    # duplicate checkin in same period
    try:
        checkin(conn, h1)
        ok("duplicate period checkin blocked", False)
    except ValueError:
        ok("duplicate period checkin blocked", True)

    # uncheck removes today's
    uncheck(conn, h1)
    ok(
        "uncheck drops to grace state",
        effective_due(conn, _habit_row(conn, h1)) is True
        and streak(conn, _habit_row(conn, h1)) == 2,
    )

    # weekdays: Sat/Sun grace; every weekday of the last 2 weeks checked
    sat = next(
        (
            now - timedelta(days=d)
            for d in range(8)
            if (now - timedelta(days=d)).weekday() == 5
        ),
        None,
    )
    for d in range(1, 15):
        day = now - timedelta(days=d)
        if day.weekday() < 5:
            checkin(conn, h3, at=day.isoformat())
    ok("weekday chain streak 10", streak(conn, _habit_row(conn, h3)) == 10)
    sat_h = _habit_row(conn, h3)
    ok("saturday not a due period", not is_due_period(sat_h, sat))

    # interval cadence: two 6h periods back
    checkin(conn, h4, at=(now - timedelta(hours=7)).isoformat())
    checkin(conn, h4, at=(now - timedelta(hours=1)).isoformat())
    ok("interval streak 2", streak(conn, _habit_row(conn, h4)) == 2)

    # mode deferral
    set_mode(conn, "yellow")
    ok(
        "red-effort deferred in yellow",
        _habit_row(conn, h2)["effort"] in DEFERRED["yellow"]
        and not effective_due(conn, _habit_row(conn, h2)),
    )
    ok("yellow-effort still due in yellow", "yellow" not in DEFERRED["yellow"])
    set_mode(conn, "red")
    names = [s["name"] for s in due_list(conn)]
    ok("red mode defers yellow+red", "Weekly deep clean" not in names)
    set_mode(conn, "green")

    # telemetry
    log_reading(conn, "solar_w", 812.5, "W")
    log_reading(conn, "solar_w", 40.0, "W")
    log_reading(conn, "batt_v", 13.1, "V")
    st = telemetry_stats(conn)
    ok(
        "telemetry stats computed",
        st["solar_w"]["max"] == 812.5
        and st["solar_w"]["min"] == 40.0
        and st["batt_v"]["count"] == 1,
    )

    # report window
    rep = {r["id"]: r for r in report(conn, days=3)}
    ok("report counts due periods", rep[h1]["due_periods"] == 3)

    # events recorded for audit
    ev = recent_events(conn, 1000)
    ok(
        "audit events exist",
        any(e["kind"] == "habit.check" and "agent:ci" in e["message"] for e in ev),
    )

    # edit + archive
    edit_habit(conn, h2, name="Weekly deep clean (biz)")
    ok("edit applies", _habit_row(conn, h2)["name"].endswith("(biz)"))
    edit_habit(conn, h2, archived=True)
    ok(
        "archived habit leaves active list",
        all(x["id"] != h2 for x in active_habits(conn)),
    )

    # CLI json roundtrip (subagent surface)
    out = []
    rc = cli(["--db", tmp, "due", "--json"], write=out.append)
    ok("cli due --json parses", rc == 0 and isinstance(json.loads("".join(out)), list))

    # SOS: detection, dedupe, ack
    sconn = connect(tmp)  # separate conn: fresh habits DB semantics
    s1 = add_habit(sconn, "SOS daily habit", "daily", "green")
    ok("sos scan clean when nothing wrong", sos_scan(sconn) == [])
    # simulate missed daily: backdate habit, leave yesterday+today unchecked
    sconn.execute(
        "UPDATE habits SET created=? WHERE id=?",
        (iso_utc(now_utc() - timedelta(days=5)), s1),
    )
    sconn.commit()
    filed = sos_scan(sconn)
    ok("missed_daily detected", any(c == "missed_daily" for _, c, _ in filed))
    filed2 = sos_scan(sconn)
    ok(
        "sos dedupe — second scan files nothing",
        filed2 == [] or all(c != "missed_daily" for _, c, _ in filed2),
    )
    sid = sos_pending(sconn)[0]["id"]
    ok("sos ack works", "acknowledged" in sos_ack(sconn, sid))
    ok("sos re-ack rejected", "already" in sos_ack(sconn, sid))
    # stale telemetry: log one reading 3 days old
    log_reading(sconn, "stale_key", 1.0)
    sconn.execute(
        "UPDATE readings SET ts=? WHERE key='stale_key'",
        (iso_utc(now_utc() - timedelta(hours=72)),),
    )
    sconn.commit()
    filed3 = sos_scan(sconn)
    ok("stale telemetry detected", any(c == "telemetry_stale" for _, c, _ in filed3))
    sconn.close()

    conn.close()
    for suffix in ("", "-wal", "-shm"):
        try:  # best-effort: Windows may hold WAL files briefly after close
            os.unlink(tmp + suffix)
        except OSError:
            pass
    write(f"selftest: {len(fails)} failure(s)" if fails else "selftest: all green")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(cli())
