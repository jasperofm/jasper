"""Timeline Editor: preview player, zoomable timeline, in/out points, saved segments, export of selections."""
import json
import threading
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLineEdit, QPushButton,
                               QSlider, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
                               QMessageBox, QDoubleSpinBox)

from .. import db, media
from ..jobs import queue
from .player import Player
from .widgets import Timeline, card, confirm, fmt_dur, label

SPEEDS = ["0.25×", "0.5×", "1×", "1.5×", "2×"]


class EditorPage(QWidget):
    analysisDone = Signal(int, object, str)     # video id, analysis dict, error

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.vid = None
        self.video = None
        self._analyzing = set()
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)

        top = QHBoxLayout()
        self.title = label("Timeline Editor", "h1")
        top.addWidget(self.title)
        top.addStretch()
        self.pick = QComboBox()
        self.pick.setMinimumWidth(280)
        self.pick.setToolTip("Choose which library video to edit")
        top.addWidget(self.pick)
        root.addLayout(top)
        self.sub = label("Open a video from the Library (double-click it) or pick one above.", "muted")
        root.addWidget(self.sub)

        split = QSplitter(Qt.Vertical)
        upper = QSplitter()
        pc, pl = card(margins=10)
        self.player = Player()
        pl.addWidget(self.player)
        speed_row = QHBoxLayout()
        speed_row.addWidget(label("Speed", "muted"))
        self.speed = QComboBox()
        self.speed.addItems(SPEEDS)
        self.speed.setCurrentText("1×")
        self.speed.setToolTip("Playback speed (preview only; exports keep normal speed)")
        speed_row.addWidget(self.speed)
        speed_row.addSpacing(16)
        speed_row.addWidget(label("Reframe", "muted"))
        self.reframe = QSlider(Qt.Horizontal)
        self.reframe.setRange(0, 100)
        self.reframe.setValue(50)
        self.reframe.setFixedWidth(160)
        self.reframe.setToolTip("Where the vertical crop sits in a wide video: 0 = left edge, 50 = centre, "
                                "100 = right edge. Used when a preset crops to fill.")
        self.reframe_lbl = label("Centre", "muted")
        speed_row.addWidget(self.reframe)
        speed_row.addWidget(self.reframe_lbl)
        speed_row.addStretch()
        pl.addLayout(speed_row)
        upper.addWidget(pc)

        sc, sl = card()
        sc.setMinimumWidth(330)
        sl.addWidget(label("Saved segments", "h2"))
        sl.addWidget(label("Mark IN (I) and OUT (O), then press Enter to save. Saved segments are used by Manual "
                           "and Hybrid clip modes, or export them directly.", "muted", wrap=True))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Label", "Start", "End", "Length"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked)
        sl.addWidget(self.table, 1)
        r1 = QHBoxLayout()
        self.btn_add = QPushButton("Save IN→OUT")
        self.btn_add.setToolTip("Save the current IN/OUT range as a segment (Enter)")
        self.btn_del = QPushButton("Delete")
        self.btn_del.setObjectName("danger")
        self.btn_del.setToolTip("Delete the selected segments (⌫)")
        self.btn_prev_sel = QPushButton("Preview")
        self.btn_prev_sel.setToolTip("Play just the IN→OUT range or the selected segment (P)")
        for b in (self.btn_add, self.btn_prev_sel, self.btn_del):
            r1.addWidget(b)
        sl.addLayout(r1)
        r2 = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.setToolTip("Export preset for these segments")
        r2.addWidget(self.preset, 1)
        self.btn_export = QPushButton("Export segments")
        self.btn_export.setObjectName("primary")
        self.btn_export.setToolTip("Queue the selected segments (or all saved segments) for export")
        r2.addWidget(self.btn_export)
        sl.addLayout(r2)
        upper.addWidget(sc)
        upper.setStretchFactor(0, 1)
        split.addWidget(upper)

        tc, tl = card(margins=12)
        bar = QHBoxLayout()
        self.in_lbl = label("IN –", "muted")
        self.out_lbl = label("OUT –", "muted")
        self.len_lbl = label("", "muted")
        self.btn_in = QPushButton("Set IN (I)")
        self.btn_out = QPushButton("Set OUT (O)")
        self.btn_clear = QPushButton("Clear")
        self.goto = QLineEdit()
        self.goto.setPlaceholderText("Go to  mm:ss")
        self.goto.setFixedWidth(110)
        self.goto.setToolTip("Type a timestamp like 1:23 or 1:02:03.5 and press Enter")
        self.btn_analyze = QPushButton("Analyze")
        self.btn_analyze.setToolTip("Find scene changes and loudness so they show on the timeline")
        self.btn_fit = QPushButton("Zoom to fit")
        self.btn_zin = QPushButton("+")
        self.btn_zin.setToolTip("Zoom in (=). Mouse wheel zooms too; shift+wheel pans.")
        self.btn_zout = QPushButton("−")
        self.btn_zout.setToolTip("Zoom out (-)")
        for w in (self.btn_in, self.btn_out, self.btn_clear, self.in_lbl, self.out_lbl, self.len_lbl):
            bar.addWidget(w)
        bar.addStretch()
        for w in (self.goto, self.btn_analyze, self.btn_zout, self.btn_zin, self.btn_fit):
            bar.addWidget(w)
        for b in (self.btn_in, self.btn_out, self.btn_clear, self.btn_analyze, self.btn_fit, self.btn_zin,
                  self.btn_zout, self.btn_add, self.btn_del, self.btn_prev_sel):
            b.setFocusPolicy(Qt.NoFocus)
        tl.addLayout(bar)
        self.timeline = Timeline()
        tl.addWidget(self.timeline)
        split.addWidget(tc)
        split.setStretchFactor(0, 1)
        root.addWidget(split, 1)

        # wiring
        self.pick.activated.connect(lambda i: self.open_video(self.pick.itemData(i)))
        self.player.positionChanged.connect(self.timeline.set_position)
        self.player.durationChanged.connect(self._dur_known)
        self.timeline.seek.connect(self.player.seek)
        self.timeline.inOutChanged.connect(self._in_out_dragged)
        self.timeline.segmentClicked.connect(self._segment_clicked)
        self.speed.currentTextChanged.connect(lambda s: self.player.player.setPlaybackRate(float(s[:-1])))
        self.reframe.valueChanged.connect(self._reframe_changed)
        self.btn_in.clicked.connect(self.set_in)
        self.btn_out.clicked.connect(self.set_out)
        self.btn_clear.clicked.connect(self.clear_in_out)
        self.btn_add.clicked.connect(self.add_segment)
        self.btn_del.clicked.connect(self.delete_segments)
        self.btn_prev_sel.clicked.connect(self.preview_selection)
        self.btn_export.clicked.connect(self.export_segments)
        self.btn_analyze.clicked.connect(self.analyze)
        self.btn_fit.clicked.connect(self.timeline.zoom_fit)
        self.btn_zin.clicked.connect(lambda: self._zoom(0.5))
        self.btn_zout.clicked.connect(lambda: self._zoom(2.0))
        self.goto.returnPressed.connect(self._goto)
        self.table.itemChanged.connect(self._label_edited)
        self.table.itemSelectionChanged.connect(self._table_sel)
        self.table.cellDoubleClicked.connect(lambda r, c: c > 0 and self._jump_row(r))
        self.analysisDone.connect(self._analysis_done)

        keys = {"Space": self.player.toggle, "I": self.set_in, "O": self.set_out, "Return": self.add_segment,
                "Enter": self.add_segment, "Backspace": self.delete_segments, "Delete": self.delete_segments,
                "Left": lambda: self.player.step(-1), "Right": lambda: self.player.step(1),
                "Shift+Left": lambda: self.player.jump(-5), "Shift+Right": lambda: self.player.jump(5),
                "P": self.preview_selection, "F": self.player.toggle_fullscreen, "M": self.player.toggle_mute,
                "=": lambda: self._zoom(0.5), "-": lambda: self._zoom(2.0), "Home": lambda: self.player.seek(0)}
        for k, fn in keys.items():
            sc_ = QShortcut(QKeySequence(k), self)
            sc_.setContext(Qt.WidgetWithChildrenShortcut)
            sc_.activated.connect(fn)
        self.reload_presets()
        self.reload_videos()
        self._set_enabled(False)

    # ---- loading
    def reload_presets(self):
        cur = self.preset.currentText() or db.setting("default_preset")
        self.preset.clear()
        self.preset.addItems(list(db.presets()))
        i = self.preset.findText(cur)
        self.preset.setCurrentIndex(max(0, i))

    def reload_videos(self):
        self.pick.blockSignals(True)
        self.pick.clear()
        self.pick.addItem("Choose a video…", None)
        for r in db.q("SELECT id,name FROM videos ORDER BY added_at DESC LIMIT 500"):
            self.pick.addItem(r["name"], r["id"])
        if self.vid:
            i = self.pick.findData(self.vid)
            if i < 0:
                v = db.q1("SELECT name FROM videos WHERE id=?", (self.vid,))
                if v:
                    self.pick.addItem(v["name"], self.vid)
                    i = self.pick.count() - 1
            self.pick.setCurrentIndex(max(0, i))
        self.pick.blockSignals(False)

    def _set_enabled(self, on):
        for w in (self.btn_in, self.btn_out, self.btn_clear, self.btn_add, self.btn_del, self.btn_prev_sel,
                  self.btn_export, self.btn_analyze, self.goto, self.reframe):
            w.setEnabled(on)

    def open_video(self, vid):
        if not vid:
            return
        v = db.q1("SELECT * FROM videos WHERE id=?", (vid,))
        if not v:
            return
        self.vid, self.video = vid, v
        self.reload_videos()
        self.title.setText(v["name"])
        from pathlib import Path
        missing = not Path(v["path"]).exists()
        self.sub.setText("⚠︎ The source file is missing or its drive is disconnected: " + v["path"] if missing else
                         f"{fmt_dur(v['duration'])} · {v['width']}×{v['height']} · "
                         f"{(v['fps'] or 0):.2f} fps · {v['path']}")
        an = json.loads(v["analysis"]) if v["analysis"] else {}
        self.timeline.set_video(v["duration"], an.get("scenes"), an.get("rms"))
        self.player.load(None if missing else v["path"], v["fps"])
        self.btn_analyze.setText("Re-analyze" if an else "Analyze")
        self.reload_segments()
        self._update_in_out_labels()
        self._set_enabled(not missing)

    def _dur_known(self, d):
        if self.video and d > 0 and abs(d - self.video["duration"]) > 1:
            self.timeline.duration = d

    # ---- in/out
    def set_in(self):
        if not self.vid:
            return
        t = self.player.pos()
        out = self.timeline.out_pt
        if out is not None and out <= t:
            out = None
        self.timeline.set_in_out(t, out)
        self._update_in_out_labels()

    def set_out(self):
        if not self.vid:
            return
        t = self.player.pos()
        a = self.timeline.in_pt
        if a is not None and a >= t:
            a = None
        self.timeline.set_in_out(a, t)
        self._update_in_out_labels()

    def clear_in_out(self):
        self.timeline.set_in_out(None, None)
        self._update_in_out_labels()

    def _in_out_dragged(self, a, b):
        self._update_in_out_labels()

    def _update_in_out_labels(self):
        a, b = self.timeline.in_pt, self.timeline.out_pt
        self.in_lbl.setText(f"IN {fmt_dur(a, ms=True)}" if a is not None else "IN –")
        self.out_lbl.setText(f"OUT {fmt_dur(b, ms=True)}" if b is not None else "OUT –")
        self.len_lbl.setText(f"· {b - a:.1f}s selected" if a is not None and b is not None and b > a else "")

    # ---- segments
    def reload_segments(self):
        rows = db.q("SELECT * FROM segments WHERE video_id=? AND kind='keep' ORDER BY start", (self.vid,)) \
            if self.vid else []
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            it = QTableWidgetItem(r["label"] or f"Segment {i + 1}")
            it.setData(Qt.UserRole, r["id"])
            self.table.setItem(i, 0, it)
            for c, txt in ((1, fmt_dur(r["start"], ms=True)), (2, fmt_dur(r["end"], ms=True)),
                           (3, f"{r['end'] - r['start']:.1f}s")):
                x = QTableWidgetItem(txt)
                x.setFlags(x.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(i, c, x)
        self.table.blockSignals(False)
        self.timeline.segments = [(r["id"], r["start"], r["end"], r["label"] or "") for r in rows]
        self.timeline.update()
        self.btn_export.setText(f"Export segments ({len(rows)})" if rows else "Export IN→OUT")

    def add_segment(self):
        a, b = self.timeline.in_pt, self.timeline.out_pt
        if not self.vid or a is None or b is None or b - a < 0.5:
            QMessageBox.information(self, "Mark a range first",
                                    "Set an IN point (I) and an OUT point (O) at least half a second apart, "
                                    "then save it.")
            return
        n = db.q1("SELECT COUNT(*) c FROM segments WHERE video_id=?", (self.vid,))["c"] + 1
        db.run("INSERT INTO segments(video_id,start,end,label,kind,created_at) VALUES(?,?,?,?, 'keep', ?)",
               (self.vid, round(a, 3), round(b, 3), f"Segment {n}", time.time()))
        self.clear_in_out()
        self.reload_segments()

    def _selected_segment_ids(self):
        return [self.table.item(i.row(), 0).data(Qt.UserRole) for i in self.table.selectionModel().selectedRows()]

    def delete_segments(self):
        ids = self._selected_segment_ids()
        if not ids:
            return
        if not confirm(self, "Delete segments", f"Delete {len(ids)} saved segment(s)? The video is not changed."):
            return
        db.many("DELETE FROM segments WHERE id=?", [(i,) for i in ids])
        self.reload_segments()

    def _label_edited(self, item):
        if item.column() == 0:
            db.run("UPDATE segments SET label=? WHERE id=?", (item.text().strip(), item.data(Qt.UserRole)))
            self.reload_segments()

    def _table_sel(self):
        ids = self._selected_segment_ids()
        self.timeline.selected = ids[0] if ids else None
        self.timeline.update()

    def _segment_clicked(self, sid):
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.UserRole) == sid:
                self.table.selectRow(r)
                r_ = db.q1("SELECT start,end FROM segments WHERE id=?", (sid,))
                self.timeline.set_in_out(r_["start"], r_["end"])
                self._update_in_out_labels()

    def _jump_row(self, row):
        sid = self.table.item(row, 0).data(Qt.UserRole)
        r = db.q1("SELECT start FROM segments WHERE id=?", (sid,))
        if r:
            self.player.seek(r["start"])

    def preview_selection(self):
        ids = self._selected_segment_ids()
        if ids:
            r = db.q1("SELECT start,end FROM segments WHERE id=?", (ids[0],))
            self.player.play_range(r["start"], r["end"])
        elif self.timeline.in_pt is not None and self.timeline.out_pt is not None:
            self.player.play_range(self.timeline.in_pt, self.timeline.out_pt)

    def export_segments(self):
        if not self.vid:
            return
        ids = self._selected_segment_ids()
        if ids:
            ranges = [(r["start"], r["end"]) for r in db.q(
                f"SELECT start,end FROM segments WHERE id IN ({','.join('?' * len(ids))}) ORDER BY start", ids)]
        else:
            ranges = [(r["start"], r["end"]) for r in db.q(
                "SELECT start,end FROM segments WHERE video_id=? AND kind='keep' ORDER BY start", (self.vid,))]
        if not ranges and self.timeline.in_pt is not None and self.timeline.out_pt is not None:
            ranges = [(self.timeline.in_pt, self.timeline.out_pt)]
        if not ranges:
            QMessageBox.information(self, "Nothing to export", "Mark an IN and OUT point or save a segment first.")
            return
        name = self.preset.currentText()
        preset = dict(db.presets()[name])
        preset["reframe_x"] = self.reframe.value() / 100
        project = db.project_name(self.video["project_id"])
        queue.add_manual_jobs(self.vid, ranges, name, preset, project)
        QMessageBox.information(self, "Added to queue",
                                f"{len(ranges)} clip(s) were added to the export queue with “{name}”. "
                                "Watch progress on the Queue page.")

    def _reframe_changed(self, v):
        self.reframe_lbl.setText("Centre" if v == 50 else ("Left " if v < 50 else "Right ") + f"{abs(v - 50) * 2}%")

    # ---- analysis
    def analyze(self):
        if not self.vid or self.vid in self._analyzing:
            return
        v = self.video
        self._analyzing.add(v["id"])
        self.btn_analyze.setEnabled(False)
        self.btn_analyze.setText("Analyzing…")

        def work():
            try:
                a = media.analyze(v["path"], has_audio=bool(v["acodec"]))
                db.run("UPDATE videos SET analysis=? WHERE id=?", (media.dumps(a), v["id"]))
                self.analysisDone.emit(v["id"], a, "")
            except Exception as e:
                db.log("error", f"Analysis failed for {v['name']}: {e}")
                self.analysisDone.emit(v["id"], None, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _analysis_done(self, vid, a, err):
        self._analyzing.discard(vid)
        if vid != self.vid:
            return
        self.btn_analyze.setEnabled(True)
        if err:
            self.btn_analyze.setText("Analyze")
            QMessageBox.warning(self, "Analysis failed", err)
            return
        self.btn_analyze.setText("Re-analyze")
        self.timeline.set_analysis(a.get("scenes"), a.get("rms"))

    # ---- misc
    def _zoom(self, k):
        tl = self.timeline
        c = tl.position if tl.v0 <= tl.position <= tl.v1 else (tl.v0 + tl.v1) / 2
        span = max(1.0, min(tl.duration, (tl.v1 - tl.v0) * k))
        tl.v0 = max(0.0, min(tl.duration - span, c - span / 2))
        tl.v1 = tl.v0 + span
        tl.update()

    def _goto(self):
        txt = self.goto.text().strip()
        try:
            parts = [float(p) for p in txt.split(":")]
            t = 0.0
            for p in parts:
                t = t * 60 + p
            self.player.seek(t)
            self.goto.clear()
        except ValueError:
            self.goto.selectAll()

    def leave(self):
        self.player.pause()
