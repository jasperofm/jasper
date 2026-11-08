"""Workflow Automation: watch folders that import and process new videos by themselves, plus schedule status."""
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .. import db, production
from .widgets import card, confirm, label


class AddWatchDialog(QDialog):
    def __init__(self, parent, path):
        super().__init__(parent)
        self.setWindowTitle("Add watch folder")
        lay = QVBoxLayout(self)
        f = QFormLayout()
        f.addRow("Folder", label(path, wrap=True))
        self.profile = QComboBox()
        self.profile.addItem("Last used Clip Generator settings", "")
        for r in db.q("SELECT name FROM profiles ORDER BY name"):
            self.profile.addItem(r["name"], r["name"])
        self.profile.addItem("Only import (don't generate clips)", "__none__")
        f.addRow("Production profile", self.profile)
        self.project = QComboBox()
        self.project.setEditable(True)
        self.project.addItems([r["name"] for r in db.projects()])
        f.addRow("Project", self.project)
        self.auto_post = QCheckBox("Autopilot: approve finished clips and post them automatically")
        f.addRow("", self.auto_post)
        lay.addLayout(f)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class AutomationPage(QWidget):
    navigate = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        root.addWidget(label("Workflow Automation", "h1"))
        root.addWidget(label("Drop videos into a watch folder (for example on your external SSD) and Reels Cutter "
                             "imports them and runs the chosen production profile automatically. Folders are "
                             "checked every minute; files still being copied are skipped until they stop growing.",
                             "muted", wrap=True))
        wc, wl = card()
        h = QHBoxLayout()
        h.addWidget(label("Watch folders", "h2"))
        h.addStretch()
        self.btn_add = QPushButton("Add folder…")
        self.btn_add.setObjectName("primary")
        self.btn_toggle = QPushButton("Pause / resume")
        self.btn_scan = QPushButton("Scan now")
        self.btn_rm = QPushButton("Remove")
        self.btn_rm.setObjectName("danger")
        for b in (self.btn_scan, self.btn_toggle, self.btn_rm, self.btn_add):
            h.addWidget(b)
        wl.addLayout(h)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Folder", "Profile", "Project", "State", "Last checked", "Posting"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setMinimumHeight(220)
        wl.addWidget(self.table)
        self.msg = label("", "muted", wrap=True)
        wl.addWidget(self.msg)
        root.addWidget(wc)

        sc, sl = card()
        sl.addWidget(label("Schedule and conditions", "h2"))
        self.sched = label("", wrap=True)
        sl.addWidget(self.sched)
        b = QPushButton("Change in Settings")
        b.clicked.connect(lambda: self.navigate.emit("settings"))
        sl.addWidget(b, alignment=Qt.AlignLeft)
        root.addWidget(sc)
        root.addStretch()

        self.btn_add.clicked.connect(self.add)
        self.btn_rm.clicked.connect(self.remove)
        self.btn_toggle.clicked.connect(self.toggle)
        self.btn_scan.clicked.connect(self.scan_now)
        self.refresh()

    def _sel(self):
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    def refresh(self):
        rows = db.q("SELECT * FROM watch_folders ORDER BY path")
        sel = self._sel()
        self.table.setRowCount(len(rows))
        from pathlib import Path
        for i, r in enumerate(rows):
            it = QTableWidgetItem(r["path"])
            it.setData(Qt.UserRole, r["id"])
            self.table.setItem(i, 0, it)
            prof = {"": "Last used settings", "__none__": "Import only"}.get(r["profile"] or "", r["profile"])
            state = ("Paused" if not r["enabled"] else "Watching" if Path(r["path"]).exists()
                     else "Folder unavailable")
            for c, t in enumerate((prof, r["project"] or "Default", state,
                                   time.strftime("%H:%M", time.localtime(r["last_scan"])) if r["last_scan"] else "–"),
                                  1):
                self.table.setItem(i, c, QTableWidgetItem(t))
            self.table.setItem(i, 5, QTableWidgetItem("Autopilot" if r["auto_post"] else "Manual"))
            if r["id"] == sel:
                self.table.selectRow(i)
        from .. import production as pr
        self.msg.setText(pr.watcher.last_msg)
        on = db.setting("schedule_enabled") == "1"
        self.sched.setText(
            (f"New exports only start between {db.setting('schedule_start')}:00 and {db.setting('schedule_end')}:00."
             if on else "No time window: processing runs whenever the app is open.")
            + (" Pauses while on battery." if db.setting("pause_on_battery") == "1" else "")
            + f" Pauses below {db.setting('internal_reserve_gb')} GB free internally and "
              f"{db.setting('min_free_gb')} GB on the export drive. Everything resumes by itself, also after a restart.")

    def add(self):
        d = QFileDialog.getExistingDirectory(self, "Folder to watch")
        if not d:
            return
        dlg = AddWatchDialog(self, d)
        if dlg.exec() != QDialog.Accepted:
            return
        db.run("INSERT OR REPLACE INTO watch_folders(path,project,profile,enabled,auto_post) VALUES(?,?,?,1,?)",
               (d, dlg.project.currentText().strip() or "Default", dlg.profile.currentData(),
                int(dlg.auto_post.isChecked())))
        production.watcher.start()
        self.refresh()

    def remove(self):
        wid = self._sel()
        if wid and confirm(self, "Stop watching", "Stop watching this folder? Its videos stay in the library and "
                                                  "nothing is deleted.", ok="Remove"):
            db.run("DELETE FROM watch_folders WHERE id=?", (wid,))
            self.refresh()

    def toggle(self):
        wid = self._sel()
        if wid:
            db.run("UPDATE watch_folders SET enabled=1-enabled WHERE id=?", (wid,))
            self.refresh()

    def scan_now(self):
        import threading
        threading.Thread(target=production.watcher.scan_all, daemon=True).start()
        self.msg.setText("Scanning… (a file is picked up on the second scan once its size stops changing)")
