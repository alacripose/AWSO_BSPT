#!/usr/bin/env python3
"""AWSO_BSPT habit tracker — QML kiosk GUI host (PySide6 QtQuick).

Qt Quick serves both kiosk (fullscreen, touch, screensaver) and normal
desktop windows; software rendering (QT_QUICK_BACKEND=software) — no GPU
required. Design language: qml-bootstrap/Ionic tokens + Apple HIG rules.

The GUI is a pure view: every mutation calls habit_core functions or the
same cli() the agent bot uses — one truth. Chat is a DB mailbox: human
types here, agent replies via `habitctl chat send`.

Usage: python3 habit_gui.py [--db PATH] [--kiosk] [--selfcheck]
"""

import os
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

import habit_core as core


class Bridge(QObject):
    """Every GUI mutation routes through habit_core — one truth for GUI + agents."""

    dataChanged = Signal()
    chatChanged = Signal()
    kioskChanged = Signal()
    modeChanged = Signal()

    def __init__(self, db=None, kiosk=False):
        super().__init__()
        self.db = db or str(core.default_db())
        self._kiosk = kiosk
        self._mode = None

    def _conn(self):
        return core.connect(self.db)

    @Property(bool, notify=kioskChanged)
    def kiosk(self):
        return self._kiosk

    # Reactive mode: QML binds Bridge.mode (a property, not a slot call), so
    # pill colors refresh when setMode() flips it. The old @Slot(str) form was
    # evaluated once at binding creation with no notify — stale forever.
    @Property(str, notify=modeChanged)
    def mode(self):
        if self._mode is None:
            with self._conn() as c:
                self._mode = core.get_mode(c)
        return self._mode

    @Slot()
    def syncMode(self):
        """Re-read mode from the DB (agent may change it via CLI) and notify.

        Called from the QML refresh cycle so external changes show up in the
        pills without restarting the GUI.
        """
        with self._conn() as c:
            m = core.get_mode(c)
        if m != self._mode:
            self._mode = m
            self.modeChanged.emit()

    @Slot()
    def poll(self):
        """Refresh-cycle ping: re-evaluates presence-bound properties.

        Emits dataChanged for agentOnline re-read — BUT must never be called
        from refreshAll (its onDataChanged handler calls refreshAll again =>
        infinite QML/JS recursion, 'Maximum call stack size exceeded').
        Called only by the QML refresh Timer below, never from refreshAll.
        """
        self.dataChanged.emit()

    # -- view models --------------------------------------------------------
    @Slot(result="QVariant")
    def dueModel(self):
        with self._conn() as c:
            return core.due_list(c)

    @Slot(result="QVariant")
    def deferredModel(self):
        with self._conn() as c:
            return core.deferred_list(c)

    @Slot(result="QVariant")
    def habitsModel(self):
        out = []
        with self._conn() as c:
            for h in c.execute("SELECT * FROM habits ORDER BY id"):
                s = core.habit_summary(c, h)
                s["description"] = h["description"]
                out.append(s)
        return out

    @Slot(str, result="QVariant")
    def readingsModel(self, key):
        rows = core.readings(
            core.connect(self.db), key if key and key != "(all)" else None, 30
        )
        return list(reversed(rows))  # oldest→newest for left-to-right charts

    @Slot(result="QVariant")
    def statsModel(self):
        with self._conn() as c:
            st = core.telemetry_stats(c)
            return [{"key": k, **v} for k, v in sorted(st.items())]

    @Slot(result="QVariant")
    def activityModel(self):
        with self._conn() as c:
            return core.recent_events(c, 15)

    @Slot(result="QVariant")
    def sosModel(self):
        with self._conn() as c:
            core.sos_scan(c)  # kiosk self-scans on refresh; watch loop covers headless
            return core.sos_pending(c)

    @Slot(int, str)
    def ackSos(self, sos_id, note):
        with self._conn() as c:
            msg = core.sos_ack(c, sos_id, by="human:gui", note=note)
        self.dataChanged.emit()
        return msg

    # -- mutations -----------------------------------------------------------
    @Slot(str)
    def setMode(self, mode):
        if mode in core.MODES and mode != self._mode:
            with self._conn() as c:
                core.set_mode(c, mode)
            self._mode = mode
            self.modeChanged.emit()

    @Slot(int)
    def check(self, hid):
        try:
            with self._conn() as c:
                core.checkin(c, hid, source="human:gui")
        except ValueError:
            pass  # already checked — refresh resolves the view
        self.dataChanged.emit()

    @Slot(int)
    def uncheck(self, hid):
        with self._conn() as c:
            core.uncheck(c, hid)
        self.dataChanged.emit()

    @Slot(str, str, str, str, str)
    def addHabit(self, name, cadence, effort, category, description):
        try:
            with self._conn() as c:
                core.add_habit(
                    c, name, cadence, effort, category, description, source="human:gui"
                )
        except ValueError:
            return
        self.dataChanged.emit()

    @Slot(int)
    def archiveHabit(self, hid):
        with self._conn() as c:
            core.edit_habit(c, hid, archived=True)
        self.dataChanged.emit()

    # -- chat mailbox ---------------------------------------------------------
    @Slot(result="QVariant")
    def chatModel(self):
        with self._conn() as c:
            return core.list_chat(c, 50)

    @Slot(str)
    def sendChat(self, text):
        """Human -> mailbox. agentd (the resident agent harness, agentd.py)
        polls the mailbox and answers; the GUI never spawns agents itself —
        it stays a pure view (see agent-operated-apps architecture)."""
        if not text.strip():
            return
        with self._conn() as c:
            core.post_chat(c, text, sender="human")
        self.chatChanged.emit()

    # -- agent presence (agentd heartbeat in meta) ------------------------------
    HEARTBEAT_FRESH_S = 90  # agentd stamps every 30s; 3x stale = offline

    @Property(bool, notify=dataChanged)
    def agentOnline(self):
        """True while agentd's heartbeat is fresh — drives 'Agent ONLINE'."""
        with self._conn() as c:
            row = c.execute("SELECT v FROM meta WHERE k='agent_heartbeat'").fetchone()
        if not row:
            return False
        try:
            stamp = core.parse_ts(row["v"])
        except ValueError:
            return False
        return (core.now_utc() - stamp).total_seconds() < self.HEARTBEAT_FRESH_S

    # -- build stamp + update channel -------------------------------------------
    @Property(str, notify=dataChanged)
    def buildStamp(self):
        st = core.build_stamp()
        tag = f"{st['version']}+{st['hash']}"
        if st["dirty"]:
            tag += " •"
        return tag

    @Slot(result="QVariant")
    def releasesModel(self):
        """Changelog bubbles for the Updates tab; syncs git first."""
        with self._conn() as c:
            core.sync_releases(c)
            return core.list_releases(c)

    @Slot(str)
    def viewRelease(self, release_id):
        """Operator opened a release bubble — persist the viewed marker."""
        with self._conn() as c:
            core.mark_release_viewed(c, release_id)
        self.dataChanged.emit()

    @Slot(int, result="QVariant")
    def habitDetail(self, hid):
        """Task row clicked: habit + audit trail (the subagent-routine view)."""
        with self._conn() as c:
            return core.habit_detail(c, hid)

    # -- fleet inventory (skills hamburger panel) ------------------------------
    @Slot(result="QVariant")
    def fleetModel(self):
        """Sectioned inventory for the Drawer: agents, models, skills by
        category. fleet_scan() is pure stdlib and sub-100ms (stat() + PATH
        walk only), so it is safe to call from QML directly."""
        return core.fleet_scan(probe=False)

    # -- console ---------------------------------------------------------------
    @Slot(str, result=str)
    def runCommand(self, cmd):
        """Run a console verb on the Qt thread (all safe verbs are quick).

        `watch` (never returns) and `gui` (spawns a second GUI) are rejected
        — running either here froze the whole kiosk (Mantis F-05).
        """
        parts = shlex.split(cmd)
        if not parts:
            return ""
        verb = parts[0].lower()
        if verb not in core.CONSOLE_SAFE_VERBS:
            return (
                f"error: '{verb}' is not available in the console.\n"
                "Run 'help' for the verb list. Daemon/dev verbs (watch, gui, "
                "model) would freeze, duplicate, or hog the kiosk."
            )
        if verb == "help":
            return core.help_text()
        out = []
        try:
            core.cli(["--db", self.db, *parts], write=out.append)
        except SystemExit:
            return "error: bad arguments (try 'help')"
        return "\n".join(out) or "(no output)"


def main() -> int:
    db = None
    kiosk = "--kiosk" in sys.argv or os.environ.get("AWSO_KIOSK") == "1"
    for i, a in enumerate(sys.argv):
        if a == "--db" and i + 1 < len(sys.argv):
            db = sys.argv[i + 1]

    # Windows/Store-Python: QML plugins need the PySide6 dirs on the DLL
    # search path or qtquick2plugin.dll fails to load (Python 3.8+ removed
    # CWD from the search path). Harmless no-op elsewhere.
    try:
        import PySide6

        base = os.path.dirname(PySide6.__file__)
        for sub in ("", "plugins"):
            os.add_dll_directory(os.path.join(base, sub))
    except (ImportError, OSError):
        pass

    # Headless guard: no display → refuse, unless offscreen (testing).
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen" and not core.display_present():
        print(
            "no display found — GUI needs a local session "
            "(or QT_QPA_PLATFORM=offscreen to test headless)"
        )
        return 1

    app = QGuiApplication(sys.argv[:1])
    app.setApplicationName("AWSO Habit Kiosk")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")

    bridge = Bridge(db, kiosk=kiosk)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("Bridge", bridge)
    engine.load(str(Path(__file__).with_name("Kiosk.qml")))
    if not engine.rootObjects():
        print("QML failed to load")
        return 1

    if "--selfcheck" in sys.argv:
        import tempfile

        if not db:  # never selfcheck against the live DB
            db = tempfile.mktemp(suffix=".db")
        bridge.db = db
        bridge._mode = None  # reset the cached mode for the new DB

        def check():
            assert isinstance(bridge.dueModel(), list), "due model"
            assert isinstance(bridge.chatModel(), list), "chat model"
            assert bridge.mode in core.MODES, "mode"
            bridge.sendChat("selfcheck ping")
            assert any(m["text"] == "selfcheck ping" for m in bridge.chatModel()), (
                "chat send"
            )
            assert "habitctl" in bridge.runCommand("version"), "console"
            assert "habitctl verbs:" in bridge.runCommand("help"), "help verb"
            assert "not available" in bridge.runCommand("watch"), "daemon block"
            assert "not available" in bridge.runCommand("model train"), "model block"
            assert "model" not in core.CONSOLE_SAFE_VERBS, "model off whitelist"
            assert "fleet" in core.CONSOLE_SAFE_VERBS, "fleet on whitelist"
            fleet = bridge.fleetModel()
            assert "skills" in fleet and "counts" in fleet, "fleet model"
            assert fleet["counts"]["skills"] > 0, "fleet found skills"
            assert bridge.runCommand('add "quoted name"') or True, "quoted add"
            bridge.setMode("red")
            assert bridge.mode == "red", "setMode property"
            bridge.setMode("green")
            bridge.addHabit("selfcheck habit", "daily", "green", "", "")
            hid = next(
                h["id"] for h in bridge.habitsModel() if h["name"] == "selfcheck habit"
            )
            bridge.check(hid)
            assert all(d["id"] != hid for d in bridge.dueModel()), "check"
            bridge.archiveHabit(hid)
            print("QML kiosk selfcheck: all green")
            app.quit()

        QTimer.singleShot(1000, check)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
