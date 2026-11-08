"""Autopilot: drop videos in a folder and watch them go to posted clips, step by step.

Left: every video the app is working on, with the step it is at. Right: the steps for the selected video, a map of
the whole video showing which parts were turned into clips, and for each clip exactly what was changed.
"""
import json
import threading
import time
import webbrowser
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QIcon
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QHeaderView, QListWidget, QListWidgetItem, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView, QSizePolicy)

from .. import autopilot, db, posting, production
from . import theme
from .widgets import card, label

STATE_COLORS = {"done": theme.GOOD, "active": theme.INFO, "waiting": theme.BORDER, "failed": theme.BAD,
                "skipped": theme.PANEL2}
JOB_COLORS = {"done": theme.GOOD, "running": theme.INFO, "queued": theme.MUTED, "failed": theme.BAD,
              "paused": theme.WARN}


class StepsBar(QWidget):
    """Seven connected chips: Found → … → Posted, coloured by state, with the detail under each."""

    def __init__(self):
        super().__init__()
        self.steps = []
        self.setMinimumHeight(100)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_steps(self, steps):
        self.steps = steps
        self.setToolTip("\n".join(f"{n}: {d}" for n, _, d in steps if d))
        self.update()

    def paintEvent(self, e):
        if not self.steps:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = len(self.steps)
        w = self.width() / n
        cy = 18
        for i, (name, state, detail) in enumerate(self.steps):
            cx = w * i + w / 2
            if i < n - 1:
                nxt = self.steps[i + 1][1]
                p.setPen(QPen(QColor(theme.GOOD if state == "done" and nxt != "waiting" else theme.BORDER), 3))
                p.drawLine(int(cx + 12), cy, int(cx + w - 12), cy)
            col = QColor(STATE_COLORS.get(state, theme.BORDER))
            p.setPen(QPen(col, 2))
            p.setBrush(col if state in ("done", "failed", "active") else QColor(theme.PANEL))
            p.drawEllipse(QRectF(cx - 10, cy - 10, 20, 20))
            p.setPen(QColor("white"))
            f = QFont(self.font())
            f.setBold(True)
            f.setPointSizeF(9)
            p.setFont(f)
            mark = {"done": "✓", "failed": "!", "active": "…", "skipped": "–"}.get(state, str(i + 1))
            p.drawText(QRectF(cx - 10, cy - 10, 20, 20), Qt.AlignCenter, mark)
            f.setBold(state == "active")
            f.setPointSizeF(10.5)
            p.setFont(f)
            p.setPen(QColor(theme.TEXT if state != "waiting" else theme.MUTED))
            p.drawText(QRectF(cx - w / 2 + 2, cy + 14, w - 4, 18), Qt.AlignHCenter | Qt.AlignTop, name)
            f.setBold(False)
            f.setPointSizeF(9)
            p.setFont(f)
            p.setPen(QColor(theme.BAD if state == "failed" else theme.MUTED))
            p.drawText(QRectF(cx - w / 2 + 2, cy + 32, w - 4, 50), Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                       detail[:90])
        p.end()


class ClipMap(QWidget):
    """The whole source video as a bar; every clip is a coloured block at the part it was cut from."""
    picked = Signal(int)

    def __init__(self):
        super().__init__()
        self.duration = 0
        self.clips = []
        self.sel = None
        self.setMinimumHeight(66)
        self.setMouseTracking(True)

    def set_data(self, duration, clips, sel=None):
        self.duration, self.clips, self.sel = duration or 0, clips, sel
        self.update()

    def _rect(self, a, b):
        W = self.width() - 20
        x0 = 10 + W * a / self.duration
        return QRectF(x0, 14, max(4.0, W * (b - a) / self.duration), 30)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.PANEL2))
        p.drawRoundedRect(QRectF(10, 14, self.width() - 20, 30), 6, 6)
        if not self.duration:
            p.end()
            return
        f = QFont(self.font())
        f.setPointSizeF(9)
        p.setFont(f)
        p.setPen(QColor(theme.MUTED))
        for k in range(0, 7):
            t = self.duration * k / 6
            x = 10 + (self.width() - 20) * k / 6
            m = int(t)
            box = QRectF(x, 46, 60, 16) if k == 0 else QRectF(x - 60, 46, 60, 16) if k == 6 else \
                QRectF(x - 30, 46, 60, 16)
            p.drawText(box, Qt.AlignLeft if k == 0 else Qt.AlignRight if k == 6 else Qt.AlignCenter,
                       f"{m // 3600}:{m % 3600 // 60:02d}:{m % 60:02d}" if self.duration >= 3600
                       else f"{m // 60}:{m % 60:02d}")
        for i, c in enumerate(self.clips, 1):
            r = self._rect(c["start"], c["end"])
            col = QColor(JOB_COLORS.get(c["status"], theme.MUTED))
            if c["review"] == "failed_qc":
                col = QColor(theme.BAD)
            p.setPen(QPen(QColor("white"), 2) if c["id"] == self.sel else Qt.NoPen)
            col.setAlpha(230)
            p.setBrush(col)
            p.drawRoundedRect(r, 4, 4)
            if c["status"] == "running":
                pr = QRectF(r.x(), r.bottom() - 4, r.width() * (c["progress"] or 0), 4)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("white"))
                p.drawRect(pr)
            if r.width() > 14:
                p.setPen(QColor("#0f1115"))
                p.drawText(r, Qt.AlignCenter, str(i))
        p.end()

    def mousePressEvent(self, e):
        x = e.position().x()
        for c in self.clips:
            r = self._rect(c["start"], c["end"])
            if r.left() - 3 <= x <= r.right() + 3:
                self.picked.emit(c["id"])
                return

    def mouseMoveEvent(self, e):
        x = e.position().x()
        for i, c in enumerate(self.clips, 1):
            r = self._rect(c["start"], c["end"])
            if r.left() - 3 <= x <= r.right() + 3:
                self.setToolTip(f"Clip {i}: {autopilot._fmt(c['start'])}–{autopilot._fmt(c['end'])} ({c['status']})")
                self.setCursor(Qt.PointingHandCursor)
                return
        self.setToolTip("")
        self.unsetCursor()


class SetupDialog(QDialog):
    def __init__(self, parent, path):
        super().__init__(parent)
        self.setWindowTitle("Autopilot folder")
        lay = QVBoxLayout(self)
        lay.addWidget(label("Every video you put in this folder is cut into clips with the settings below, checked, "
                            "added to the posting calendar and sent to your linked accounts. No clicks needed.",
                            "muted", wrap=True))
        f = QFormLayout()
        f.addRow("Folder", label(path, wrap=True))
        self.profile = QComboBox()
        self.profile.addItem("Last used Clip Generator settings", "")
        for r in db.q("SELECT name FROM profiles ORDER BY name"):
            self.profile.addItem(r["name"], r["name"])
        f.addRow("Clip settings", self.profile)
        self.project = QComboBox()
        self.project.setEditable(True)
        self.project.addItems([r["name"] for r in db.projects()] or ["Default"])
        f.addRow("Project", self.project)
        lay.addLayout(f)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Start autopilot")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class AutopilotPage(QWidget):
    navigate = Signal(str)
    reEdit = Signal(int, float, float)

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.vid = None
        self.job = None
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(label("Autopilot", "h1"))
        top.addStretch()
        self.btn_setup = QPushButton("Add autopilot folder…")
        self.btn_setup.setObjectName("primary")
        self.btn_accounts = QPushButton("Connect accounts")
        self.btn_scan = QPushButton("Check folders now")
        self.btn_pause = QPushButton("Pause")
        for b in (self.btn_scan, self.btn_pause, self.btn_accounts, self.btn_setup):
            top.addWidget(b)
        root.addLayout(top)
        self.status = label("", wrap=True)
        self.status.setTextFormat(Qt.RichText)
        root.addWidget(self.status)

        split = QSplitter(Qt.Horizontal)
        lc, ll = card(margins=10)
        ll.addWidget(label("Videos", "h2"))
        self.videos = QListWidget()
        self.videos.setSpacing(2)
        ll.addWidget(self.videos, 1)
        split.addWidget(lc)

        rc, rl = card()
        self.title = label("Pick a video on the left", "h2")
        rl.addWidget(self.title)
        self.steps = StepsBar()
        rl.addWidget(self.steps)
        rl.addWidget(label("Which parts of the video became clips (click a block)", "muted"))
        self.map = ClipMap()
        rl.addWidget(self.map)
        mid = QHBoxLayout()
        self.clips = QListWidget()
        self.clips.setIconSize(self.clips.iconSize().__class__(64, 112))
        self.clips.setMaximumWidth(250)
        mid.addWidget(self.clips)
        right = QVBoxLayout()
        self.changes = QTableWidget(0, 2)
        self.changes.setHorizontalHeaderLabels(["What", "Change"])
        self.changes.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.changes.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.changes.verticalHeader().hide()
        self.changes.setWordWrap(True)
        self.changes.setEditTriggers(QTableWidget.NoEditTriggers)
        self.changes.setSelectionMode(QAbstractItemView.NoSelection)
        right.addWidget(self.changes, 1)
        bh = QHBoxLayout()
        self.btn_play = QPushButton("Play clip")
        self.btn_src = QPushButton("Compare with original")
        self.btn_reveal = QPushButton("Show in Finder")
        for b in (self.btn_play, self.btn_src, self.btn_reveal):
            bh.addWidget(b)
        bh.addStretch()
        right.addLayout(bh)
        mid.addLayout(right, 1)
        rl.addLayout(mid, 1)
        split.addWidget(rc)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setSizes([300, 900])
        root.addWidget(split, 1)

        ac, al = card(margins=10)
        al.addWidget(label("Activity", "h2"))
        self.log = QListWidget()
        self.log.setMaximumHeight(130)
        al.addWidget(self.log)
        root.addWidget(ac)

        self.btn_setup.clicked.connect(self.setup)
        self.btn_accounts.clicked.connect(lambda: webbrowser.open(posting.CONNECT_URL))
        self.btn_scan.clicked.connect(self.scan_now)
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.videos.currentItemChanged.connect(lambda cur, _: self.pick_video(cur.data(Qt.UserRole) if cur else None))
        self.clips.currentItemChanged.connect(lambda cur, _: self.pick_clip(cur.data(Qt.UserRole) if cur else None))
        self.map.picked.connect(self._select_clip)
        self.btn_play.clicked.connect(self.play)
        self.btn_src.clicked.connect(self.play_source)
        self.btn_reveal.clicked.connect(self.reveal)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    # ---- refresh
    def refresh(self):
        if not self.isVisible() and self.videos.count():
            return
        try:
            self._status()
            self._videos()
            if self.vid:
                self._detail(keep=True)
            self._log()
        except Exception as e:
            db.log("error", f"Autopilot page refresh failed: {e}")

    def _status(self):
        folders = db.q("SELECT * FROM watch_folders WHERE auto_post=1")
        on = [f for f in folders if f["enabled"]]
        key = bool((db.setting("ayrshare_api_key") or "").strip())
        plats = json.loads(db.setting("post_platforms", "[]"))
        def tick(ok):
            return f"<span style='color:{theme.GOOD if ok else theme.WARN}'>{'✓' if ok else '○'}</span>"
        parts = [f"{tick(bool(on))} Autopilot folder: " + (", ".join(Path(f['path']).name for f in on)
                                                           if on else "none yet (Add autopilot folder…)"),
                 f"{tick(key)} Posting: " + ("Ayrshare connected" if key else
                                             "add your Ayrshare key in Auto-Posting, then Connect accounts"),
                 f"{tick(bool(plats))} Posting to: " + (", ".join(posting.PLATFORMS.get(p, p) for p in plats)
                                                        or "nothing picked")
                 + f" · {db.setting('posts_per_day', '6')} a day"]
        from ..jobs import queue
        if queue.paused:
            parts.append(f"<span style='color:{theme.WARN}'>Paused</span>")
        self.btn_pause.setText("Resume" if queue.paused else "Pause")
        self.status.setText(" &nbsp;&nbsp; ".join(parts))

    def _videos(self):
        rows = autopilot.videos()
        cur = self.vid
        self.videos.blockSignals(True)
        self.videos.clear()
        for v in rows:
            active = next(((n, d) for n, s, d in v["steps"] if s in ("active", "failed")), None)
            last_done = [n for n, s, _ in v["steps"] if s == "done"]
            line = (f"{active[0]}: {active[1]}" if active else
                    "Waiting" if not last_done else f"{last_done[-1]} ✓")
            failed = any(s == "failed" for _, s, _ in v["steps"])
            it = QListWidgetItem(("⚡ " if v["autopilot"] else "") + v["name"] + "\n" + line)
            it.setData(Qt.UserRole, v["id"])
            it.setForeground(QColor(theme.BAD if failed else theme.TEXT))
            self.videos.addItem(it)
            if v["id"] == cur:
                self.videos.setCurrentItem(it)
        self.videos.blockSignals(False)
        if cur is None and rows:
            self.videos.setCurrentRow(0)
        self._rows = {v["id"]: v for v in rows}

    def pick_video(self, vid):
        self.vid = vid
        self.job = None
        self._detail(keep=False)

    def _detail(self, keep):
        v = (getattr(self, "_rows", {}) or {}).get(self.vid)
        if not v:
            return
        self.title.setText(("⚡ Autopilot · " if v["autopilot"] else "") + v["name"])
        self.steps.set_steps(v["steps"])
        jobs = autopilot.clips(self.vid)
        if self.job is None and jobs:
            self.job = jobs[0]["id"]
        self.map.set_data(v["duration"], jobs, self.job)
        self.clips.blockSignals(True)
        self.clips.clear()
        for i, j in enumerate(jobs, 1):
            post = db.q1("SELECT status, at FROM posts WHERE job_id=?", (j["id"],))
            st = ("held back by quality check" if j["review"] == "failed_qc" else
                  f"editing {j['progress']:.0%}" if j["status"] == "running" else
                  {"queued": "waiting", "done": "ready", "failed": "failed"}.get(j["status"], j["status"]))
            if post:
                st = {"planned": "in calendar", "scheduled": "scheduled", "failed": "post failed",
                      "sending": "sending"}.get(post["status"], st)
                if post["status"] == "scheduled" and post["at"] <= time.time():
                    st = "posted"
            it = QListWidgetItem(f"Clip {i}  ·  {autopilot._fmt(j['start'])}–{autopilot._fmt(j['end'])}\n{st}")
            it.setData(Qt.UserRole, j["id"])
            if j["thumb"] and Path(j["thumb"]).exists():
                it.setIcon(QIcon(QPixmap(j["thumb"])))
            self.clips.addItem(it)
            if j["id"] == self.job:
                self.clips.setCurrentItem(it)
        self.clips.blockSignals(False)
        self._changes()

    def _select_clip(self, jid):
        self.job = jid
        for i in range(self.clips.count()):
            if self.clips.item(i).data(Qt.UserRole) == jid:
                self.clips.setCurrentRow(i)
        self.map.set_data(self.map.duration, self.map.clips, jid)
        self._changes()

    def pick_clip(self, jid):
        if jid is None:
            return
        self.job = jid
        self.map.set_data(self.map.duration, self.map.clips, jid)
        self._changes()

    def _changes(self):
        j = db.q1("SELECT * FROM jobs WHERE id=?", (self.job,)) if self.job else None
        rows = autopilot.changes(j) if j else []
        self.changes.setRowCount(len(rows))
        for i, (what, det) in enumerate(rows):
            a = QTableWidgetItem(what)
            a.setForeground(QColor(theme.MUTED))
            self.changes.setItem(i, 0, a)
            self.changes.setItem(i, 1, QTableWidgetItem(det))
        self.changes.resizeRowsToContents()
        ok = bool(j and j["out_path"] and Path(j["out_path"]).exists())
        self.btn_play.setEnabled(ok)
        self.btn_reveal.setEnabled(ok)
        self.btn_src.setEnabled(bool(j))

    def _log(self):
        rows = autopilot.activity(30)
        sig = tuple(r["ts"] for r in rows[:3])
        if getattr(self, "_logsig", None) == sig:
            return
        self._logsig = sig
        self.log.clear()
        for r in rows:
            it = QListWidgetItem(time.strftime("%H:%M:%S", time.localtime(r["ts"])) + "   " +
                                 r["message"].split("\n")[0][:220])
            if r["level"] == "error":
                it.setForeground(QColor(theme.BAD))
            elif r["level"] == "warning":
                it.setForeground(QColor(theme.WARN))
            self.log.addItem(it)

    # ---- actions
    def setup(self):
        d = QFileDialog.getExistingDirectory(self, "Folder for autopilot")
        if not d:
            return
        dlg = SetupDialog(self, d)
        if dlg.exec() != QDialog.Accepted:
            return
        db.run("INSERT INTO watch_folders(path,project,profile,enabled,auto_post) VALUES(?,?,?,1,1) "
               "ON CONFLICT(path) DO UPDATE SET project=excluded.project, profile=excluded.profile, enabled=1, "
               "auto_post=1", (d, dlg.project.currentText().strip() or "Default", dlg.profile.currentData()))
        production.watcher.start()
        db.log("info", f"Autopilot started for {d}")
        self.refresh()

    def scan_now(self):
        production.watcher.start()
        threading.Thread(target=production.watcher.scan_all, daemon=True).start()
        db.log("info", "Checking folders now (a file is picked up once it has finished copying)")

    def toggle_pause(self):
        from ..jobs import queue
        queue.resume() if queue.paused else queue.pause()
        self.refresh()

    def _job(self):
        return db.q1("SELECT * FROM jobs WHERE id=?", (self.job,)) if self.job else None

    def play(self):
        j = self._job()
        if j and j["out_path"]:
            from .library import open_path
            open_path(j["out_path"])

    def play_source(self):
        """Opens the original video in the Timeline Editor with this clip's part marked, to compare."""
        j = self._job()
        if j:
            self.reEdit.emit(j["video_id"], j["start"], j["end"])

    def reveal(self):
        j = self._job()
        if j and j["out_path"]:
            import subprocess
            import sys
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", j["out_path"]])
            else:
                from .library import open_path
                open_path(str(Path(j["out_path"]).parent))
