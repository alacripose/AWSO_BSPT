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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import habit_core as core

from PySide6.QtCore import QObject, Property, Signal, Slot, QTimer
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtGui import QGuiApplication


class Bridge(QObject):
    """Every GUI mutation routes through habit_core — one truth for GUI + agents."""

    dataChanged = Signal()
    chatChanged = Signal()
    kioskChanged = Signal()

    def __init__(self, db=None, kiosk=False):
        super().__init__()
        self.db = db or str(core.default_db())
        self._kiosk = kiosk

    def _conn(self):
        return core.connect(self.db)

    @Property(bool, notify=kioskChanged)
    def kiosk(self):
        return self._kiosk

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
        rows = core.readings(core.connect(self.db),
                             key if key and key != "(all)" else None, 30)
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

    @Slot(result=str)
    def mode(self):
        with self._conn() as c:
            return core.get_mode(c)

    # -- mutations -----------------------------------------------------------
    @Slot(str)
    def setMode(self, mode):
        if mode in core.MODES:
            with self._conn() as c:
                core.set_mode(c, mode)
            self.dataChanged.emit()

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
                core.add_habit(c, name, cadence, effort, category,
                               description, source="human:gui")
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
        if text.strip():
            with self._conn() as c:
                core.post_chat(c, text, sender="human")
            self.chatChanged.emit()

    # -- console ---------------------------------------------------------------
    @Slot(str, result=str)
    def runCommand(self, cmd):
        out = []
        try:
            core.cli(["--db", self.db, *cmd.split()], write=out.append)
        except SystemExit:
            return "error: bad arguments"
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
    if (os.environ.get("QT_QPA_PLATFORM") != "offscreen"
            and not core.display_present()):
        print("no display found — GUI needs a local session "
              "(or QT_QPA_PLATFORM=offscreen to test headless)")
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

        def check():
            assert isinstance(bridge.dueModel(), list), "due model"
            assert isinstance(bridge.chatModel(), list), "chat model"
            assert bridge.mode() in core.MODES, "mode"
            bridge.sendChat("selfcheck ping")
            assert any(m["text"] == "selfcheck ping"
                       for m in bridge.chatModel()), "chat send"
            assert "habitctl" in bridge.runCommand("version"), "console"
            bridge.addHabit("selfcheck habit", "daily", "green", "", "")
            hid = [h["id"] for h in bridge.habitsModel()
                   if h["name"] == "selfcheck habit"][0]
            bridge.check(hid)
            assert all(d["id"] != hid for d in bridge.dueModel()), "check"
            bridge.archiveHabit(hid)
            print("QML kiosk selfcheck: all green")
            app.quit()
        QTimer.singleShot(1000, check)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
