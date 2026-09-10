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
CREATE TABLE IF NOT EXISTS events(
  ts TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_checkins ON checkins(habit_id, ts);
CREATE INDEX IF NOT EXISTS idx_readings ON readings(key, ts);
CREATE INDEX IF NOT EXISTS idx_events ON events(ts);
"""


def connect(db=None) -> sqlite3.Connection:
    path = Path(db) if db else default_db()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def log_event(conn, kind, message):
    conn.execute("INSERT INTO events(ts,kind,message) VALUES(?,?,?)",
                 (iso_utc(now_utc()), kind, message))


def next_id(conn) -> int:
    row = conn.execute("SELECT v FROM meta WHERE k='next_id'").fetchone()
    n = int(row["v"]) if row else 1
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('next_id',?)",
                 (str(n + 1)))
    return n


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
        (habit_id, iso_utc(pstart), iso_utc(e))).fetchone()
    return row is not None


def _habit_row(conn, habit_id):
    return conn.execute("SELECT * FROM habits WHERE id=?", (habit_id,)).fetchone()


def iter_periods(h, start: datetime, end: datetime):
    """Aligned period starts in [start, end)."""
    created_p = period_start(h, local(h["created"]))
    p = period_start(h, start)
    if p < created_p:
        p = created_p
    while p < end:
        yield p
        p += period_len(h)


def streak(conn, h, at: datetime = None) -> int:
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


def effective_due(conn, h, at: datetime = None) -> bool:
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

def add_habit(conn, name, cadence="daily", effort="green", category="",
              description="", source="human", created=None):
    if not CADENCE_RE.match(cadence):
        raise ValueError(f"bad cadence '{cadence}' "
                         "(daily|weekdays|weekly:N|interval:Nh)")
    if effort not in EFFORTS:
        raise ValueError(f"bad effort '{effort}' (green|yellow|red)")
    hid = next_id(conn)
    conn.execute(
        "INSERT INTO habits(id,name,description,cadence,effort,category,created)"
        " VALUES(?,?,?,?,?,?,?)",
        (hid, name, description, cadence, effort, category,
         iso_utc(parse_ts(created) if created else now_utc())))
    log_event(conn, "habit.add",
              f"#{hid} '{name}' cadence={cadence} effort={effort} by {source}")
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
        raise ValueError(f"already checked in this period (use --force)")
    conn.execute(
        "INSERT INTO checkins(habit_id,ts,source,note) VALUES(?,?,?,?)",
        (habit_id, ts, source, note))
    log_event(conn, "habit.check",
              f"#{habit_id} '{h['name']}' checked in by {source}")
    conn.commit()
    return streak(conn, h, local(ts))


def uncheck(conn, habit_id, at=None):
    h = _habit_row(conn, habit_id)
    if not h:
        raise ValueError(f"no habit #{habit_id}")
    if at:
        p = period_start(h, parse_ts(at))
        n = conn.execute("DELETE FROM checkins WHERE habit_id=? AND ts>=? AND ts<?",
                         (habit_id, iso_utc(p), iso_utc(p + period_len(h)))).rowcount
    else:
        row = conn.execute("SELECT ts FROM checkins WHERE habit_id=?"
                           " ORDER BY ts DESC LIMIT 1", (habit_id,)).fetchone()
        if not row:
            raise ValueError(f"no checkins for #{habit_id}")
        n = conn.execute("DELETE FROM checkins WHERE habit_id=? AND ts=?",
                         (habit_id, row["ts"])).rowcount
    log_event(conn, "habit.uncheck", f"#{habit_id} removed {n} checkin(s)")
    conn.commit()
    return n


def edit_habit(conn, habit_id, *, name=None, description=None, cadence=None,
               effort=None, category=None, archived=None):
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
        "id": h["id"], "name": h["name"], "cadence": h["cadence"],
        "effort": h["effort"], "category": h["category"],
        "archived": bool(h["archived"]),
        "streak": streak(conn, h, at),
        "due": effective_due(conn, h, at),
        "deferred": h["effort"] in DEFERRED[get_mode(conn)],
    }


def due_list(conn, at=None):
    at = at or datetime.now().astimezone()
    return [habit_summary(conn, h, at) for h in active_habits(conn)
            if effective_due(conn, h, at)]


def active_habits(conn):
    return conn.execute("SELECT * FROM habits WHERE archived=0 ORDER BY id").fetchall()


def report(conn, days=7, at=None):
    """Completion rate per active habit over the trailing window."""
    at = at or datetime.now().astimezone()
    start = at.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    out = []
    for h in active_habits(conn):
        due = done = 0
        for p in iter_periods(h, start, at):
            if is_due_period(h, p):
                due += 1
                if has_checkin(conn, h["id"], p):
                    done += 1
        out.append({"id": h["id"], "name": h["name"], "due_periods": due,
                    "completed": done,
                    "rate": round(done / due, 2) if due else None})
    return out


def log_reading(conn, key, value, unit=""):
    conn.execute("INSERT INTO readings(key,ts,value,unit) VALUES(?,?,?,?)",
                 (key, iso_utc(now_utc()), float(value), unit))
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
            " MAX(ts) last_ts, MIN(unit) unit FROM readings WHERE ts>=? GROUP BY key",
            (since,)):
        out[row["key"]] = {"count": row["n"], "min": round(row["mn"], 3),
                            "avg": round(row["av"], 3), "max": round(row["mx"], 3),
                            "unit": row["unit"], "last": row["last_ts"]}
    return out


def recent_events(conn, n=100):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (n,))]


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
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # detach: survives watch restarts
    )


def watch(db=None, interval=60, headless=False, write=print):
    conn = connect(db)
    log_event(conn, "watch.start", f"watch loop (interval={interval}s,"
                                   f" headless={headless})")
    conn.commit()
    gui_proc = None
    write(f"watch: ticking every {interval}s "
          f"({'headless' if headless else 'GUI attach on display'})")
    while True:
        if not headless and gui_proc is None and display_present():
            gui_proc = spawn_gui()
            write(f"watch: spawned GUI (pid {gui_proc.pid})")
        if gui_proc is not None and gui_proc.poll() is not None:
            write(f"watch: GUI exited (code {gui_proc.returncode}); "
                  f"will respawn next tick if a display is present")
            gui_proc = None
        _sleep(interval)


def _sleep(seconds):  # seam for selftest
    raise KeyboardInterrupt


# ---------------------------------------------------------------------------
# CLI (also the subagent control surface)
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="habitctl",
                                 description="AWSO_BSPT habit tracker")
    p.add_argument("--db", help=f"database path (default $AWSO_DB or "
                                f"{default_db().name} beside the script)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_json(sp):
        sp.add_argument("--json", action="store_true", help="machine output")
        return sp

    sp = sub.add_parser("add", help="create a habit")
    sp.add_argument("name")
    sp.add_argument("--cadence", default="daily",
                    help="daily|weekdays|weekly:N|interval:Nh")
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

    sp = sub.add_parser("events", help="recent audit events")
    sp.add_argument("--last", type=int, default=50)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("watch", help="daemon: attach GUI when display appears")
    sp.add_argument("--interval", type=int, default=60)
    sp.add_argument("--headless", action="store_true",
                    help="never spawn the GUI")

    sp = sub.add_parser("gui", help="launch the GUI now (needs a display)")
    sp = sub.add_parser("selftest")
    sp = sub.add_parser("version")
    return p


def cli(argv=None, write=print) -> int:
    args = build_parser().parse_args(argv)
    conn = connect(args.db)
    J = lambda o: write(json.dumps(o, indent=2))

    try:
        if args.cmd == "add":
            hid = add_habit(conn, args.name, args.cadence, args.effort,
                            args.category, args.description, args.source,
                            args.created)
            write(f"added habit #{hid}: {args.name} "
                  f"({args.cadence}, effort {args.effort})")

        elif args.cmd == "list":
            rows = conn.execute("SELECT * FROM habits ORDER BY id").fetchall() \
                if args.all else active_habits(conn)
            habits = [habit_summary(conn, h) for h in rows]
            if args.json:
                J(habits)
            else:
                for s in habits:
                    flag = " (archived)" if s["archived"] else ""
                    mark = "✓" if not s["due"] and not s["deferred"] else \
                        ("⊘ deferred" if s["deferred"] else "•")
                    write(f"#{s['id']:>3} {mark} {s['name']}{flag} — "
                          f"{s['cadence']}, streak {s['streak']}, "
                          f"effort {s['effort']}")

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
                J({**habit_summary(conn, h), "description": h["description"],
                   "created": h["created"]})
            else:
                s = habit_summary(conn, h)
                write(f"#{s['id']} {s['name']}\n  cadence:  {s['cadence']}"
                      f"\n  effort:   {s['effort']}  (mode {get_mode(conn)},"
                      f" deferred={s['deferred']})\n  streak:   {s['streak']}"
                      f"\n  due now:  {s['due']}"
                      f"\n  created:  {h['created']}"
                      f"\n  about:    {h['description'] or '—'}")

        elif args.cmd == "edit":
            if args.archive and args.unarchive:
                raise ValueError("pick one of --archive/--unarchive")
            edits = edit_habit(conn, args.id, name=args.name,
                               description=args.description, cadence=args.cadence,
                               effort=args.effort, category=args.category,
                               archived=True if args.archive
                               else (False if args.unarchive else None))
            write(f"habit #{args.id}: updated {sorted(edits) if edits else 'nothing'}")

        elif args.cmd == "due":
            d = due_list(conn)
            if args.json:
                J(d)
            elif not d:
                write("nothing due — all clear")
            else:
                for s in d:
                    write(f"#{s['id']:>3} • {s['name']} ({s['cadence']}, "
                          f"streak {s['streak']})")

        elif args.cmd == "streaks":
            rows = [{"id": h["id"], "name": h["name"],
                     "streak": streak(conn, h)} for h in active_habits(conn)]
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
                    rate = f"{r['rate']*100:.0f}%" if r["rate"] is not None else "—"
                    write(f"  #{r['id']:>3} {r['name'][:28]:<28} "
                          f"{r['completed']}/{r['due_periods']}  {rate}")

        elif args.cmd == "status":
            mode = get_mode(conn)
            due = due_list(conn)
            deferred = [habit_summary(conn, h) for h in active_habits(conn)
                        if h["effort"] in DEFERRED[mode] and
                        is_due_period(h, period_start(h, datetime.now().astimezone()))]
            st = telemetry_stats(conn)
            latest = {k: v["last"] for k, v in st.items()}
            payload = {"mode": mode, "due_count": len(due),
                       "deferred_count": len(deferred),
                       "due": [d["name"] for d in due],
                       "deferred": [d["name"] for d in deferred],
                       "telemetry_keys": sorted(st),
                       "time": now_utc().isoformat(timespec="seconds")}
            if args.json:
                J(payload)
            else:
                write(f"AWSO_BSPT — {payload['time']}")
                write(f"mode {mode.upper()} | due {len(due)} | "
                      f"deferred {len(deferred)}")
                for d in due:
                    write(f"  due:      {d['name']} (streak {d['streak']})")
                for d in deferred:
                    write(f"  deferred: {d['name']}")
                for k, ts in latest.items():
                    write(f"  reading:  {k} last at {ts}")

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
                    write(f"  {k}: n={v['count']} min={v['min']} "
                          f"avg={v['avg']} max={v['max']} {v['unit']}")

        elif args.cmd == "events":
            ev = recent_events(conn, args.last)
            if args.json:
                J(ev)
            else:
                for e in reversed(ev):
                    write(f"{e['ts']} [{e['kind']}] {e['message']}")

        elif args.cmd == "watch":
            watch(args.db, args.interval, args.headless, write)
            return 0

        elif args.cmd == "gui":
            if not display_present():
                write("no display found — GUI needs a real session "
                      "(or set QT_QPA_PLATFORM=offscreen to test headless)")
                return 1
            proc = spawn_gui()
            write(f"GUI launched (pid {proc.pid})")

        elif args.cmd == "selftest":
            return selftest(write)

        elif args.cmd == "version":
            write(f"habitctl {VERSION}")

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
    h1 = add_habit(conn, "Morning inventory scan", "daily", "green",
                   created=(now - timedelta(days=10)).isoformat())
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
    ok("streak 2 after two backfilled days",
       streak(conn, _habit_row(conn, h1)) == 2)
    ok("today is grace — unchecked doesn't break",
       effective_due(conn, _habit_row(conn, h1)) is True)
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
    ok("uncheck drops to grace state",
       effective_due(conn, _habit_row(conn, h1)) is True and
       streak(conn, _habit_row(conn, h1)) == 2)

    # weekdays: Sat/Sun grace; every weekday of the last 2 weeks checked
    sat = next((now - timedelta(days=d) for d in range(8)
                if (now - timedelta(days=d)).weekday() == 5), None)
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
    ok("red-effort deferred in yellow",
       _habit_row(conn, h2)["effort"] in DEFERRED["yellow"] and
       not effective_due(conn, _habit_row(conn, h2)))
    ok("yellow-effort still due in yellow",
       "yellow" not in DEFERRED["yellow"])
    set_mode(conn, "red")
    names = [s["name"] for s in due_list(conn)]
    ok("red mode defers yellow+red", "Weekly deep clean" not in names)
    set_mode(conn, "green")

    # telemetry
    log_reading(conn, "solar_w", 812.5, "W")
    log_reading(conn, "solar_w", 40.0, "W")
    log_reading(conn, "batt_v", 13.1, "V")
    st = telemetry_stats(conn)
    ok("telemetry stats computed",
       st["solar_w"]["max"] == 812.5 and st["solar_w"]["min"] == 40.0
       and st["batt_v"]["count"] == 1)

    # report window
    rep = {r["id"]: r for r in report(conn, days=3)}
    ok("report counts due periods", rep[h1]["due_periods"] == 3)

    # events recorded for audit
    ev = recent_events(conn, 1000)
    ok("audit events exist",
       any(e["kind"] == "habit.check" and "agent:ci" in e["message"] for e in ev))

    # edit + archive
    edit_habit(conn, h2, name="Weekly deep clean (biz)")
    ok("edit applies", _habit_row(conn, h2)["name"].endswith("(biz)"))
    edit_habit(conn, h2, archived=True)
    ok("archived habit leaves active list",
       all(x["id"] != h2 for x in active_habits(conn)))

    # CLI json roundtrip (subagent surface)
    out = []
    rc = cli(["--db", tmp, "due", "--json"], write=out.append)
    ok("cli due --json parses", rc == 0 and isinstance(json.loads("".join(out)), list))

    conn.close()
    for suffix in ("", "-wal", "-shm"):
        try:  # best-effort: Windows may hold WAL files briefly after close
            os.unlink(tmp + suffix)
        except OSError:
            pass
    write(f"selftest: {len(fails)} failure(s)" if fails
          else f"selftest: all green")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(cli())
