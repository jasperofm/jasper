"""Dashboard: live numbers from the database, queue progress, storage, recent projects and activity."""
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QListWidget, QListWidgetItem, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget, QScrollArea)

from .. import db, hardware
from ..jobs import queue
from . import theme
from .widgets import StatCard, card, fmt_dur, fmt_size, label


class Dashboard(QWidget):
    navigate = Signal(str)
    importRequested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        body.setObjectName("page")
        scroll.setWidget(body)
        outer.addWidget(scroll)
        root = QVBoxLayout(body)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        top = QHBoxLayout()
        col = QVBoxLayout()
        col.addWidget(label("Dashboard", "h1"))
        self.greet = label("", "muted")
        col.addWidget(self.greet)
        top.addLayout(col)
        top.addStretch()
        self.btn_import = QPushButton("Import videos")
        self.btn_gen = QPushButton("Generate clips")
        self.btn_gen.setObjectName("primary")
        top.addWidget(self.btn_import)
        top.addWidget(self.btn_gen)
        root.addLayout(top)

        grid = QGridLayout()
        grid.setSpacing(14)
        self.c_videos = StatCard("Videos in library", theme.INFO)
        self.c_analyzed = StatCard("Analyzed videos", theme.ACCENT2)
        self.c_clips = StatCard("Clips generated", theme.GOOD)
        self.c_approved = StatCard("Approved clips", theme.GOOD)
        self.c_active = StatCard("Exporting now", theme.INFO)
        self.c_pending = StatCard("Pending jobs", theme.WARN)
        self.c_failed = StatCard("Failed jobs", theme.BAD)
        self.c_storage = StatCard("Free on export drive", theme.ACCENT)
        for i, c in enumerate((self.c_videos, self.c_analyzed, self.c_clips, self.c_approved, self.c_active,
                               self.c_pending, self.c_failed, self.c_storage)):
            grid.addWidget(c, i // 4, i % 4)
        root.addLayout(grid)

        q, ql = card()
        h = QHBoxLayout()
        h.addWidget(label("Processing", "h2"))
        h.addStretch()
        self.btn_pause = QPushButton("Pause")
        self.btn_queue = QPushButton("Open queue")
        h.addWidget(self.btn_pause)
        h.addWidget(self.btn_queue)
        ql.addLayout(h)
        self.qbar = QProgressBar()
        self.qbar.setRange(0, 1000)
        ql.addWidget(self.qbar)
        self.qtext = label("", "muted", wrap=True)
        ql.addWidget(self.qtext)
        self.alert = label("", wrap=True)
        self.alert.setStyleSheet(f"color:{theme.WARN};")
        ql.addWidget(self.alert)
        root.addWidget(q)

        row = QHBoxLayout()
        row.setSpacing(14)
        pc, pl = card()
        pl.addWidget(label("Recent projects", "h2"))
        self.projects = QListWidget()
        self.projects.setMinimumHeight(170)
        pl.addWidget(self.projects)
        row.addWidget(pc, 1)
        ac, al = card()
        al.addWidget(label("Recent activity", "h2"))
        self.activity = QListWidget()
        self.activity.setMinimumHeight(170)
        al.addWidget(self.activity)
        row.addWidget(ac, 1)
        perf, pfl = card()
        pfl.addWidget(label("Performance", "h2"))
        self.perf = label("", "muted", wrap=True)
        pfl.addWidget(self.perf)
        pfl.addStretch()
        self.btn_sys = QPushButton("System monitor")
        pfl.addWidget(self.btn_sys)
        row.addWidget(perf, 1)
        root.addLayout(row)
        root.addStretch()

        self.btn_import.clicked.connect(self.importRequested.emit)
        self.btn_gen.clicked.connect(lambda: self.navigate.emit("generator"))
        self.btn_queue.clicked.connect(lambda: self.navigate.emit("queue"))
        self.btn_sys.clicked.connect(lambda: self.navigate.emit("system"))
        self.btn_pause.clicked.connect(lambda: (queue.resume() if queue.paused else queue.pause(), self.refresh()))
        self.projects.itemDoubleClicked.connect(lambda it: self.navigate.emit("projects"))
        self._last_slow = 0
        self.refresh()

    def refresh(self):
        v = db.q1("SELECT COUNT(*) n, SUM(analysis IS NOT NULL) a, SUM(duration) d, SUM(status='missing') m "
                  "FROM videos")
        j = db.q1("SELECT SUM(status='done') done, SUM(review='approved') ok, SUM(status='running') run, "
                  "SUM(status='queued') q, SUM(status='failed') f, SUM(status='done' AND finished_at>?) today "
                  "FROM jobs", (time.time() - 86400,))
        planning = db.q1("SELECT COUNT(*) c FROM batch_videos WHERE state IN ('pending','planning')")["c"]
        self.c_videos.set(f"{v['n']:,}", f"{fmt_dur(v['d'] or 0)} of footage" +
                          (f" · {v['m']} missing" if v["m"] else ""))
        self.c_analyzed.set(f"{v['a'] or 0:,}", f"{100 * (v['a'] or 0) / v['n']:.0f}% of library" if v["n"] else "")
        self.c_clips.set(f"{j['done'] or 0:,}", f"{j['today'] or 0} in the last 24 h")
        rev = db.q1("SELECT COUNT(*) c FROM jobs WHERE review='awaiting' AND status='done'")["c"]
        self.c_approved.set(f"{j['ok'] or 0:,}", f"{rev} awaiting review" if rev else "")
        self.c_active.set(j["run"] or 0, "paused" if queue.paused else "")
        self.c_pending.set(f"{(j['q'] or 0):,}", f"+ {planning} video(s) being planned" if planning else "")
        self.c_failed.set(j["f"] or 0, "see Queue to retry" if j["f"] else "")
        if time.time() - self._last_slow > 10:
            self._last_slow = time.time()
            out = db.setting("output_dir")
            fg = hardware.free_gb(out)
            self.c_storage.set(f"{fg:.0f} GB" if fg is not None else "–", Path(out).name or out)
            self._slow_lists()
        c = db.q1("SELECT SUM(CASE WHEN status IN ('queued','running','done') THEN end-start END) tot, "
                  "SUM(CASE WHEN status='done' THEN end-start WHEN status='running' THEN (end-start)*progress END) "
                  "got FROM jobs WHERE status IN ('queued','running') OR (status='done' AND batch_id IN "
                  "(SELECT batch_id FROM jobs WHERE status IN ('queued','running')))")
        self.qbar.setValue(int(1000 * (c["got"] or 0) / c["tot"]) if c["tot"] else 0)
        if j["run"] or j["q"] or planning:
            self.qtext.setText(f"{j['run'] or 0} exporting, {j['q'] or 0} waiting"
                               + (f", {planning} video(s) being analyzed" if planning else "")
                               + ("  ·  PAUSED" if queue.paused else f"  ·  about {fmt_dur(queue.eta_seconds())} left"))
        else:
            self.qtext.setText("Nothing is processing. Import videos and press Generate clips.")
        self.alert.setText(queue.blocked_reason or "")
        self.alert.setVisible(bool(queue.blocked_reason))
        self.btn_pause.setText("Resume" if queue.paused else "Pause")
        hour = time.localtime().tm_hour
        self.greet.setText(("Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening")
                           + f" · profile: {db.setting('profile', 'safe').capitalize()} mode")

    def _slow_lists(self):
        self.projects.clear()
        for r in db.q("SELECT p.name, COUNT(DISTINCT v.id) n, (SELECT COUNT(*) FROM jobs j JOIN batches b ON "
                      "b.id=j.batch_id WHERE b.project_id=p.id AND j.status='done') clips, "
                      "MAX(COALESCE(v.added_at, p.created_at)) t FROM projects p LEFT JOIN videos v ON "
                      "v.project_id=p.id GROUP BY p.id ORDER BY t DESC LIMIT 8"):
            self.projects.addItem(f"{r['name']}  ·  {r['n']} videos  ·  {r['clips']} clips")
        self.activity.clear()
        for r in db.q("SELECT ts, level, message FROM log ORDER BY id DESC LIMIT 30"):
            it = QListWidgetItem(f"{time.strftime('%H:%M', time.localtime(r['ts']))}  {r['message'][:140]}")
            if r["level"] == "error":
                it.setForeground(Qt.red)
            self.activity.addItem(it)
        if not self.activity.count():
            self.activity.addItem("No activity yet.")
        speed = queue.speed
        rep = hardware.last_report()
        txt = []
        if speed:
            clip = sum(a for a, _ in speed)
            wall = sum(b for _, b in speed)
            txt.append(f"Recent exports: {clip / wall:.2f}× real time per worker ({len(speed)} clips measured).")
        else:
            txt.append("No exports measured yet this session.")
        if rep:
            r = rep.get("results", {})
            sw = r.get("software_h264")
            txt.append(f"Benchmark ({time.strftime('%d %b', time.localtime(rep['ts']))}): software H.264 "
                       f"{sw}× real time" if sw else "Benchmark has no export result.")
        else:
            txt.append("This Mac hasn't been benchmarked yet (System Monitor → Run benchmark).")
        total, avail, pct = hardware.memory()
        txt.append(f"Memory: {pct:.0f}% used of {total / 1e9:.0f} GB.")
        self.perf.setText("\n".join(txt))
