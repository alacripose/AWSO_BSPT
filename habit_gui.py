#!/usr/bin/env python3
"""AWSO_BSPT habit tracker — GUI (PySide6).

One QMainWindow, four tabs (Today / Habits / Telemetry / Console). A pure
*view* over habit_core: every mutation calls habit_core functions or the
same cli() the subagents use, against one SQLite file (WAL = safe to share).

Headless rule: refuses to start without a display unless
QT_QPA_PLATFORM=offscreen (used by --selfcheck). Qt Widgets raster backend
needs no OpenGL — fine on cheap framebuffer devices.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import habit_core as core

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

# Themes carried over from the parent portal (portal_simulator.py THEMES).
THEMES = {
    "Classic": {"bg": "#15223a", "panel": "#1c2a44", "window": "#edf2f8",
                "border": "#42618e", "text": "#1d2730", "muted": "#536275",
                "accent": "#3d6fa8", "accent_text": "#ffffff", "title": "#263a59"},
    "Modern": {"bg": "#132e40", "panel": "#1b3d54", "window": "#eef7fb",
               "border": "#6da5c5", "text": "#1d2730", "muted": "#526872",
               "accent": "#3e7fa5", "accent_text": "#ffffff", "title": "#426174"},
    "Dark": {"bg": "#161616", "panel": "#242424", "window": "#242424",
             "border": "#383838", "text": "#f4f4f4", "muted": "#9a9a9a",
             "accent": "#6ea8ff", "accent_text": "#0d0d0d", "title": "#2b2b2b"},
    "Light": {"bg": "#dfe6ee", "panel": "#f7f9fb", "window": "#ffffff",
              "border": "#9ba9b8", "text": "#1d2730", "muted": "#687783",
              "accent": "#4d86c5", "accent_text": "#ffffff", "title": "#6f879e"},
    "Traditional": {"bg": "#211b1c", "panel": "#292425", "window": "#322c2a",
                    "border": "#5c4c45", "text": "#f2e9e2", "muted": "#a99a91",
                    "accent": "#c8794b", "accent_text": "#211b1c", "title": "#382f2c"},
}
MODE_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def qss(t):
    return f"""
    QMainWindow, QWidget {{ background: {t['bg']}; color: {t['text']};
        font-family: "Segoe UI", "Noto Sans", sans-serif; font-size: 10pt; }}
    QTabWidget::pane {{ border: 1px solid {t['border']}; border-radius: 4px;
        background: {t['panel']}; }}
    QTabBar::tab {{ background: {t['window']}; color: {t['text']};
        padding: 6px 14px; margin-right: 2px; border: 1px solid {t['border']};
        border-bottom: none; border-top-left-radius: 4px;
        border-top-right-radius: 4px; }}
    QTabBar::tab:selected {{ background: {t['panel']}; color: {t['accent']};
        font-weight: bold; }}
    QPushButton {{ background: {t['window']}; color: {t['text']};
        border: 1px solid {t['border']}; border-radius: 3px; padding: 5px 12px; }}
    QPushButton:hover {{ border-color: {t['accent']}; }}
    QPushButton:disabled {{ color: {t['muted']}; }}
    QLineEdit, QComboBox {{ background: {t['window']}; color: {t['text']};
        border: 1px solid {t['border']}; border-radius: 3px; padding: 4px 8px; }}
    QTableWidget {{ background: {t['panel']}; color: {t['text']};
        border: 1px solid {t['border']}; gridline-color: {t['border']}; }}
    QTableWidget::item:selected {{ background: {t['accent']};
        color: {t['accent_text']}; }}
    QHeaderView::section {{ background: {t['window']}; color: {t['text']};
        border: 1px solid {t['border']}; padding: 4px; font-weight: 600; }}
    QTextEdit {{ background: {t['panel']}; color: {t['text']};
        border: 1px solid {t['border']}; font-family: Consolas, monospace;
        font-size: 9pt; }}
    QLabel#title {{ font-size: 14pt; font-weight: 600; }}
    QLabel#muted {{ color: {t['muted']}; }}
    QLabel#stat {{ color: {t['muted']}; font-weight: 600; }}
    """


class HabitGUI(QMainWindow):
    def __init__(self, db=None, theme="Classic"):
        super().__init__()
        self.db = db
        self.theme_name = theme
        self.setWindowTitle("AWSO_BSPT — Habit Tracker")
        self.resize(880, 560)
        self.conn = lambda: core.connect(self.db)  # fresh conn per op (WAL)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.tabs.addTab(self._today_tab(), "Today")
        self.tabs.addTab(self._habits_tab(), "Habits")
        self.tabs.addTab(self._telemetry_tab(), "Telemetry")
        self.tabs.addTab(self._console_tab(), "Console")

        self.timer = QTimer(self)  # ponytail: poll refresh; event-driven later
        self.timer.timeout.connect(self.refresh)
        self.timer.start(30000)
        self.refresh()

    # -- helpers ------------------------------------------------------------
    def _pill(self, text):
        lbl = QLabel(text)
        lbl.setObjectName("stat")
        return lbl

    def _mode_buttons(self, layout):
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Operator mode</b>"))
        for mode in core.MODES:
            b = QPushButton(f"{MODE_EMOJI[mode]} {mode.upper()}")
            b.setObjectName(f"mode_{mode}")
            b.clicked.connect(lambda _=False, m=mode: self.set_mode(m))
            row.addWidget(b)
        row.addStretch()
        self.mode_lbl = QLabel()
        row.addWidget(self.mode_lbl)
        layout.addLayout(row)

    # -- Today tab ------------------------------------------------------------
    def _today_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self._mode_buttons(v)
        self.today_stat = QLabel()
        v.addWidget(self.today_stat)
        v.addWidget(QLabel("<b>Due now</b>"))
        self.due_table = QTableWidget(0, 4)
        self.due_table.setHorizontalHeaderLabels(
            ["ID", "Habit", "Cadence", "Streak"])
        self.due_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        v.addWidget(self.due_table)
        row = QHBoxLayout()
        b = QPushButton("✓ Check selected")
        b.clicked.connect(self.check_selected)
        row.addWidget(b)
        b2 = QPushButton("⊘ Uncheck selected")
        b2.clicked.connect(self.uncheck_selected)
        row.addWidget(b2)
        row.addStretch()
        v.addLayout(row)
        self.deferred_lbl = QLabel()
        self.deferred_lbl.setObjectName("muted")
        v.addWidget(self.deferred_lbl)

        v.addWidget(QLabel("<b>Recent activity</b> (you + your agent)"))
        self.activity_table = QTableWidget(0, 3)
        self.activity_table.setHorizontalHeaderLabels(["Time", "Event", "Detail"])
        self.activity_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        v.addWidget(self.activity_table)
        return w

    # -- Habits tab -----------------------------------------------------------
    def _habits_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.habit_table = QTableWidget(0, 6)
        self.habit_table.setHorizontalHeaderLabels(
            ["ID", "Name", "Cadence", "Effort", "Category", "Streak"])
        self.habit_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.habit_table.setSelectionBehavior(QTableWidget.SelectRows)
        v.addWidget(self.habit_table)
        row = QHBoxLayout()
        b = QPushButton("✎ Load selected into form")
        b.clicked.connect(self.load_selected)
        row.addWidget(b)
        b = QPushButton("📁 Archive selected")
        b.clicked.connect(self.archive_selected)
        row.addWidget(b)
        row.addStretch()
        v.addLayout(row)

        form = QFormLayout()
        self.f_id = QLabel("—")
        self.f_name = QLineEdit()
        self.f_cadence = QComboBox()
        self.f_cadence.setEditable(True)
        self.f_cadence.addItems(["daily", "weekdays", "weekly:7", "interval:6h"])
        self.f_effort = QComboBox()
        self.f_effort.addItems(core.EFFORTS)
        self.f_category = QLineEdit()
        self.f_desc = QLineEdit()
        form.addRow("Editing", self.f_id)
        form.addRow("Name", self.f_name)
        form.addRow("Cadence", self.f_cadence)
        form.addRow("Effort", self.f_effort)
        form.addRow("Category", self.f_category)
        form.addRow("Description", self.f_desc)
        v.addLayout(form)
        row2 = QHBoxLayout()
        b = QPushButton("＋ Add as new")
        b.clicked.connect(self.save_habit)
        row2.addWidget(b)
        b = QPushButton("💾 Save changes")
        b.clicked.connect(self.save_habit)
        row2.addWidget(b)
        row2.addStretch()
        v.addLayout(row2)
        return w

    # -- Telemetry tab ----------------------------------------------------------
    def _telemetry_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        row.addWidget(QLabel("Key"))
        self.tele_key = QComboBox()
        self.tele_key.setMinimumWidth(180)
        row.addWidget(self.tele_key)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh)
        row.addWidget(b)
        row.addStretch()
        v.addLayout(row)
        self.tele_table = QTableWidget(0, 4)
        self.tele_table.setHorizontalHeaderLabels(
            ["Timestamp", "Key", "Value", "Unit"])
        self.tele_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        v.addWidget(self.tele_table)
        self.tele_stats = QLabel()
        self.tele_stats.setObjectName("muted")
        v.addWidget(self.tele_stats)
        return w

    # -- Console tab -------------------------------------------------------------
    def _console_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("Direct command line — same verbs as the CLI/"
                           "subagents (add, due, check 1, mode yellow, …)"))
        self.console_out = QTextEdit()
        self.console_out.setReadOnly(True)
        v.addWidget(self.console_out, 1)
        row = QHBoxLayout()
        self.console_in = QLineEdit()
        self.console_in.setPlaceholderText("command  (e.g. status, due --json)")
        self.console_in.returnPressed.connect(self.run_console)
        row.addWidget(self.console_in, 1)
        b = QPushButton("Run")
        b.clicked.connect(self.run_console)
        row.addWidget(b)
        v.addLayout(row)
        return w

    # -- actions (all via habit_core: one truth) --------------------------------
    def set_mode(self, mode):
        with self.conn() as c:
            core.set_mode(c, mode)
        self.log(f"mode set to {mode}")
        self.refresh()

    def check_selected(self):
        hid = self._selected_id(self.due_table)
        if hid is None:
            return
        try:
            with self.conn() as c:
                s = core.checkin(c, hid, source="human:gui")
            self.log(f"checked #{hid} (streak {s})")
        except ValueError as e:
            self.log(f"error: {e}")
        self.refresh()

    def uncheck_selected(self):
        hid = self._selected_id(self.due_table)
        if hid is None:
            return
        with self.conn() as c:
            n = core.uncheck(c, hid)
        self.log(f"removed {n} checkin(s) from #{hid}")
        self.refresh()

    def load_selected(self):
        hid = self._selected_id(self.habit_table)
        if hid is None:
            return
        with self.conn() as c:
            h = core._habit_row(c, hid)
        self.f_id.setText(str(hid))
        self.f_name.setText(h["name"])
        self.f_cadence.setCurrentText(h["cadence"])
        self.f_effort.setCurrentText(h["effort"])
        self.f_category.setText(h["category"])
        self.f_desc.setText(h["description"])

    def archive_selected(self):
        hid = self._selected_id(self.habit_table)
        if hid is None:
            return
        with self.conn() as c:
            core.edit_habit(c, hid, archived=True)
        self.log(f"archived #{hid}")
        self.refresh()

    def save_habit(self):
        is_edit = self.f_id.text() not in ("—", "")
        try:
            with self.conn() as c:
                if is_edit:
                    core.edit_habit(
                        c, int(self.f_id.text()), name=self.f_name.text(),
                        cadence=self.f_cadence.currentText(),
                        effort=self.f_effort.currentText(),
                        category=self.f_category.text(),
                        description=self.f_desc.text())
                    self.log(f"edited #{self.f_id.text()}")
                else:
                    hid = core.add_habit(
                        c, self.f_name.text(), self.f_cadence.currentText(),
                        self.f_effort.currentText(), self.f_category.text(),
                        self.f_desc.text(), source="human:gui")
                    self.log(f"added #{hid} {self.f_name.text()}")
        except ValueError as e:
            QMessageBox.warning(self, "Invalid", str(e))
            return
        self._clear_form()
        self.refresh()

    def _clear_form(self):
        self.f_id.setText("—")
        for f in (self.f_name, self.f_category, self.f_desc):
            f.clear()

    def run_console(self):
        cmd = self.console_in.text().strip()
        if not cmd:
            return
        self.console_in.clear()
        out = []
        try:
            rc = core.cli(["--db", str(self.db_path()), *cmd.split()],
                           write=out.append)
        except SystemExit as e:  # argparse bad args
            out.append(f"exit: {e}")
            rc = 1
        self.log(f"$ {cmd}")
        self.log("\n".join(out) or "(no output)")
        if rc:
            self.log(f"(exit {rc})")
        self.refresh()

    def log(self, text):
        ts = datetime.now().strftime("%H:%M:%S")
        self.console_out.append(f"[{ts}] {text}")

    def db_path(self):
        return self.db if self.db else core.default_db()

    @staticmethod
    def _selected_id(table):
        sel = table.selectionModel().selectedRows()
        if not sel:
            return None
        return int(table.item(sel[0].row(), 0).text())

    # -- refresh (read path) ------------------------------------------------------
    def refresh(self):
        try:
            self._refresh()
        except Exception as e:  # keep GUI alive on transient DB errors
            self.log(f"refresh error: {e}")

    def _refresh(self):
        conn = self.conn()
        try:
            mode = core.get_mode(conn)
            self.mode_lbl.setText(
                f"{MODE_EMOJI[mode]} {mode.upper()}")
            due = core.due_list(conn)
            self.today_stat.setText(
                f"{len(due)} due · {sum(1 for s in due if s['streak'] >= 3)} on fire")

            self.due_table.setRowCount(0)
            for r, s in enumerate(due):
                self.due_table.insertRow(r)
                for col, val in enumerate(
                        (s["id"], s["name"], s["cadence"], s["streak"])):
                    self.due_table.setItem(r, col, QTableWidgetItem(str(val)))

            deferred = [core.habit_summary(conn, h) for h in core.active_habits(conn)
                        if h["effort"] in core.DEFERRED[mode]]
            if deferred:
                self.deferred_lbl.setText(
                    "Deferred by mode: " +
                    ", ".join(f"#{d['id']} {d['name']}" for d in deferred))
            else:
                self.deferred_lbl.setText("")

            self.activity_table.setRowCount(0)
            for r, e in enumerate(core.recent_events(conn, 15)):
                self.activity_table.insertRow(r)
                ts = e["ts"][11:19] if len(e["ts"]) >= 19 else e["ts"]
                for col, val in enumerate((ts, e["kind"], e["message"])):
                    self.activity_table.setItem(r, col, QTableWidgetItem(str(val)))

            self.habit_table.setRowCount(0)
            for r, h in enumerate(conn.execute(
                    "SELECT * FROM habits ORDER BY id")):
                self.habit_table.insertRow(r)
                vals = (h["id"], h["name"], h["cadence"], h["effort"],
                        h["category"], core.streak(conn, h))
                for col, val in enumerate(vals):
                    item = QTableWidgetItem(str(val))
                    if h["archived"]:
                        item.setText(item.text() + " (archived)")
                    self.habit_table.setItem(r, col, item)

            keys = [r["key"] for r in conn.execute(
                "SELECT DISTINCT key FROM readings ORDER BY key")]
            self.tele_key.clear()
            self.tele_key.addItem("(all)")
            self.tele_key.addItems(keys)
            rows = core.readings(conn, None if self.tele_key.currentIndex() <= 0
                                 else self.tele_key.currentText(), 30)
            self.tele_table.setRowCount(0)
            for r, row in enumerate(rows):
                self.tele_table.insertRow(r)
                for col, val in enumerate(
                        (row["ts"], row["key"], row["value"], row["unit"])):
                    self.tele_table.setItem(r, col, QTableWidgetItem(str(val)))
            st = core.telemetry_stats(conn)
            self.tele_stats.setText(" · ".join(
                f"{k}: min {v['min']} avg {v['avg']} max {v['max']} {v['unit']}"
                for k, v in st.items()) or "no telemetry yet")
        finally:
            conn.close()


def main() -> int:
    args = sys.argv[1:]
    selfcheck = "--selfcheck" in args
    db = None
    for i, a in enumerate(args):
        if a == "--db" and i + 1 < len(args):
            db = args[i + 1]

    # Headless guard: no display → refuse, unless offscreen (testing).
    if (os.environ.get("QT_QPA_PLATFORM") != "offscreen"
            and not core.display_present()):
        print("no display found — GUI needs a local session "
              "(or QT_QPA_PLATFORM=offscreen to test headless)")
        return 1

    app = QApplication(args[:1] or ["awso-gui"])  # Qt chokes on our own flags
    win = HabitGUI(db=db)
    win.show()

    if selfcheck:
        def _check():
            assert win.due_table.rowCount() >= 0, "today tab rendered"
            assert win.habit_table.columnCount() == 6, "habits tab rendered"
            assert win.tele_table.columnCount() == 4, "telemetry tab rendered"
            win.console_in.setText("version")
            win.run_console()
            assert "habitctl" in win.console_out.toPlainText(), "console ran cli"
            print("GUI selfcheck: all green")
            app.quit()
        QTimer.singleShot(500, _check)  # let first paint + refresh happen

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
