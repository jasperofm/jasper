"""Queue page: every export job with live progress, ETA, pause/resume, cancel, retry and concurrency."""
import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QPushButton, QSpinBox,
                               QTableView, QVBoxLayout, QWidget, QMenu, QProgressBar, QMessageBox)

from .. import db
from ..jobs import queue
from .library import open_path, reveal
from .tables import RowsModel
from .widgets import ProgressDelegate, confirm, fmt_dur, label

FILTERS = {"Active (queued + running)": "j.status IN ('queued','running')", "All jobs": "1=1",
           "Running": "j.status='running'", "Queued": "j.status='queued'", "Failed": "j.status='failed'",
           "Done": "j.status='done'", "Canceled": "j.status='canceled'"}
ORDER = "CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END, j.id"


def _preset_name(r):
    try:
        return json.loads(r["preset"]).get("name", "")
    except Exception:
        return ""


class QueuePage(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(label("Processing Queue", "h1"))
        top.addStretch()
        top.addWidget(label("Parallel exports", "muted"))
        self.conc = QSpinBox()
        self.conc.setRange(1, 8)
        self.conc.setValue(queue.concurrency())
        self.conc.setToolTip("How many clips export at the same time. More is faster on strong Macs but uses "
                             "more memory and heat.")
        top.addWidget(self.conc)
        self.btn_pause = QPushButton("Pause all")
        self.btn_pause.setObjectName("primary")
        self.btn_pause.setToolTip("Pause or resume every export. Paused jobs continue exactly where they stopped.")
        top.addWidget(self.btn_pause)
        root.addLayout(top)

        summary = QHBoxLayout()
        self.status = label("", "muted")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedWidth(260)
        summary.addWidget(self.status, 1)
        summary.addWidget(self.bar)
        root.addLayout(summary)
        self.warn = label("", wrap=True)
        self.warn.setStyleSheet("color:#f5b84b;")
        self.warn.hide()
        root.addWidget(self.warn)

        bar = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItems(FILTERS)
        self.filter.setCurrentText("All jobs")
        bar.addWidget(self.filter)
        bar.addStretch()
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setToolTip("Cancel the selected queued or running jobs")
        self.btn_retry = QPushButton("Retry")
        self.btn_retry.setToolTip("Run the selected failed or canceled jobs again")
        self.btn_retry_all = QPushButton("Retry all failed")
        self.btn_clear = QPushButton("Clear finished")
        self.btn_clear.setToolTip("Remove done and canceled jobs from this list. Exported files are kept.")
        self.btn_open = QPushButton("Open clip")
        for b in (self.btn_open, self.btn_cancel, self.btn_retry, self.btn_retry_all, self.btn_clear):
            bar.addWidget(b)
        root.addLayout(bar)

        cols = [
            ("Video", lambda r: r["vname"] or "(removed)"),
            ("Range", lambda r: f"{fmt_dur(r['start'])} → {fmt_dur(r['end'])}"),
            ("Length", lambda r: f"{r['end'] - r['start']:.0f}s"),
            ("Preset", _preset_name),
            ("Mode", lambda r: r["selection"] or ""),
            ("Status", lambda r: r["status"].capitalize()),
            ("Progress", lambda r: f"{(r['progress'] or 0) * 100:.0f}%"),
            ("Output / error", lambda r: r["error"] if r["status"] in ("failed",) or
             (r["error"] and r["status"] == "queued") else Path(r["out_path"] or "").name),
        ]
        self.model = RowsModel(cols, progress_col=6, status_col=5)
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setItemDelegateForColumn(6, ProgressDelegate(self.view))
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.verticalHeader().hide()
        self.view.setAlternatingRowColors(True)
        self.view.setShowGrid(False)
        h = self.view.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(7, QHeaderView.Stretch)
        self.view.setColumnWidth(6, 170)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        root.addWidget(self.view, 1)
        self.more = label("", "muted")
        root.addWidget(self.more)

        self.conc.valueChanged.connect(queue.set_concurrency)
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.filter.currentIndexChanged.connect(self.refresh)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_retry.clicked.connect(lambda: (queue.retry(self.selected_ids()), self.refresh()))
        self.btn_retry_all.clicked.connect(self.retry_all)
        self.btn_clear.clicked.connect(self.clear)
        self.btn_open.clicked.connect(self.open_clip)
        self.view.doubleClicked.connect(lambda i: self.open_clip())
        self.view.customContextMenuRequested.connect(self.menu)
        self.view.selectionModel().selectionChanged.connect(self._sel)
        self.refresh()

    def selected_rows(self):
        return [self.model.rows[i.row()] for i in self.view.selectionModel().selectedRows()]

    def selected_ids(self):
        return [r["id"] for r in self.selected_rows()]

    def _sel(self, *a):
        rows = self.selected_rows()
        st = {r["status"] for r in rows}
        self.btn_cancel.setEnabled(bool(st & {"queued", "running"}))
        self.btn_retry.setEnabled(bool(st & {"failed", "canceled"}))
        self.btn_open.setEnabled(len(rows) == 1 and rows[0]["status"] == "done")

    def refresh(self):
        where = FILTERS[self.filter.currentText()]
        rows = db.q(f"SELECT j.*, v.name vname FROM jobs j LEFT JOIN videos v ON v.id=j.video_id "
                    f"WHERE {where} AND j.hidden=0 ORDER BY {ORDER} LIMIT 3000")
        total = db.q1(f"SELECT COUNT(*) c FROM jobs j WHERE {where} AND j.hidden=0")["c"]
        self.model.set_rows(rows)
        self.more.setText(f"Showing the first 3,000 of {total:,} jobs" if total > 3000 else f"{total:,} jobs")
        c = db.q1("SELECT SUM(status='running') run, SUM(status='queued') q, SUM(status='failed') f, "
                  "SUM(status='done') d, SUM(CASE WHEN status IN ('queued','running','done') THEN end-start END) "
                  "tot, SUM(CASE WHEN status='done' THEN end-start WHEN status='running' THEN (end-start)*progress "
                  "END) got FROM jobs")
        planning = db.q1("SELECT COUNT(*) c FROM batch_videos WHERE state IN ('pending','planning')")["c"]
        run, qd, f, d = (c["run"] or 0), (c["q"] or 0), (c["f"] or 0), (c["d"] or 0)
        bits = [f"{run} running", f"{qd} queued", f"{d} done", f"{f} failed"]
        if planning:
            bits.append(f"{planning} video(s) being analyzed / planned")
        eta = queue.eta_seconds()
        if queue.paused:
            bits.append("PAUSED")
        elif run or qd:
            bits.append(f"about {fmt_dur(eta)} left")
        self.status.setText(" · ".join(bits))
        self.bar.setValue(int(1000 * (c["got"] or 0) / c["tot"]) if c["tot"] else 0)
        self.btn_pause.setText("Resume all" if queue.paused else "Pause all")
        self.warn.setVisible(bool(queue.blocked_reason))
        self.warn.setText(queue.blocked_reason or "")
        self.btn_retry_all.setEnabled(bool(f))
        self._sel()

    def toggle_pause(self):
        queue.resume() if queue.paused else queue.pause()
        self.refresh()

    def cancel(self):
        ids = [r["id"] for r in self.selected_rows() if r["status"] in ("queued", "running")]
        if ids and confirm(self, "Cancel jobs", f"Cancel {len(ids)} job(s)? Unfinished clips are discarded. "
                                                "You can retry them later.", ok="Cancel jobs"):
            queue.cancel(ids)
            self.refresh()

    def retry_all(self):
        n = queue.retry_all_failed()
        self.refresh()
        if n:
            QMessageBox.information(self, "Retrying", f"{n} failed job(s) were put back in the queue.")

    def clear(self):
        if confirm(self, "Clear finished", "Remove done and canceled jobs from the queue list?\n\n"
                                           "Exported clips stay on disk and in the Export Manager.", ok="Clear"):
            queue.clear_finished()
            self.refresh()

    def open_clip(self):
        rows = self.selected_rows()
        if len(rows) == 1 and rows[0]["status"] == "done" and Path(rows[0]["out_path"]).exists():
            open_path(rows[0]["out_path"])

    def menu(self, pos):
        rows = self.selected_rows()
        if not rows:
            return
        m = QMenu(self)
        if len(rows) == 1 and rows[0]["status"] == "done":
            m.addAction("Play clip", self.open_clip)
            m.addAction("Show in Finder", lambda: reveal(rows[0]["out_path"]))
        if self.btn_cancel.isEnabled():
            m.addAction("Cancel", self.cancel)
        if self.btn_retry.isEnabled():
            m.addAction("Retry", lambda: (queue.retry(self.selected_ids()), self.refresh()))
        if len(rows) == 1 and rows[0]["error"]:
            m.addAction("Show error…", lambda: QMessageBox.information(self, "Error", rows[0]["error"]))
        m.exec(self.view.viewport().mapToGlobal(pos))
