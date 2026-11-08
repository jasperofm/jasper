"""System Monitor: detected hardware, live CPU/memory, performance profiles, benchmark report and logs."""
import json
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget, QGridLayout)

from .. import db, hardware, paths
from ..jobs import queue
from .widgets import card, fmt_size, label


class SystemPage(QWidget):
    benchDone = Signal(object, str)
    benchMsg = Signal(str)

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
        root.setSpacing(14)
        root.addWidget(label("System Monitor", "h1"))

        grid = QGridLayout()
        grid.setSpacing(14)
        hc, hl = card()
        hl.addWidget(label("This Mac", "h2"))
        self.hw_form = QFormLayout()
        self.hw = {}
        for k in ("System", "Processor", "Cores", "Memory", "Graphics", "Video encoders", "Hardware encoding"):
            v = label("…", wrap=True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.hw[k] = v
            self.hw_form.addRow(label(k, "muted"), v)
        hl.addLayout(self.hw_form)
        grid.addWidget(hc, 0, 0)

        lc, ll = card()
        ll.addWidget(label("Live", "h2"))
        ll.addWidget(label("CPU", "muted"))
        self.cpu = QProgressBar()
        self.cpu.setRange(0, 100)
        ll.addWidget(self.cpu)
        ll.addWidget(label("Memory", "muted"))
        self.mem = QProgressBar()
        self.mem.setRange(0, 100)
        ll.addWidget(self.mem)
        self.live = label("", "muted", wrap=True)
        ll.addWidget(self.live)
        ll.addStretch()
        grid.addWidget(lc, 0, 1)

        pc, pl = card()
        pl.addWidget(label("Performance profile", "h2"))
        self.profile = QComboBox()
        for p in hardware.PROFILES:
            self.profile.addItem(hardware.PROFILE_LABELS[p], p)
        pl.addWidget(self.profile)
        self.prof_info = label("", "muted", wrap=True)
        pl.addWidget(self.prof_info)
        self.btn_apply = QPushButton("Apply profile")
        self.btn_apply.setObjectName("primary")
        pl.addWidget(self.btn_apply)
        grid.addWidget(pc, 1, 0)

        bc, bl = card()
        bl.addWidget(label("Benchmark", "h2"))
        bl.addWidget(label("Measures this Mac with real test exports (about 1–3 minutes). Results are used to "
                           "recommend settings and estimate times. Nothing is guessed.", "muted", wrap=True))
        self.bench = label("", wrap=True)
        self.bench.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bl.addWidget(self.bench)
        r = QHBoxLayout()
        self.btn_bench = QPushButton("Run benchmark")
        self.btn_report = QPushButton("Save capability report…")
        r.addWidget(self.btn_bench)
        r.addWidget(self.btn_report)
        bl.addLayout(r)
        grid.addWidget(bc, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        logc, logl = card()
        h = QHBoxLayout()
        h.addWidget(label("Log", "h2"))
        h.addStretch()
        self.log_level = QComboBox()
        self.log_level.addItems(["All", "Errors and warnings", "Errors"])
        self.btn_log_export = QPushButton("Export log…")
        h.addWidget(self.log_level)
        h.addWidget(self.btn_log_export)
        logl.addLayout(h)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(220)
        logl.addWidget(self.log)
        root.addWidget(logc)

        self.profile.currentIndexChanged.connect(self._prof_info)
        self.btn_apply.clicked.connect(self.apply_profile)
        self.btn_bench.clicked.connect(self.run_bench)
        self.btn_report.clicked.connect(self.save_report)
        self.log_level.currentIndexChanged.connect(lambda i: self.refresh_log())
        self.btn_log_export.clicked.connect(self.export_log)
        self.benchDone.connect(self._bench_done)
        self.benchMsg.connect(lambda m: self.bench.setText(m))
        self.info = None
        self._loaded = False

    def showEvent(self, e):
        super().showEvent(e)
        if not self._loaded:
            self._loaded = True
            self.load_static()

    def load_static(self):
        self.info = info = hardware.detect()
        self.hw["System"].setText(f"{info['os']} · {info['arch']}" + (f" · {info['model']}" if info["model"] else ""))
        self.hw["Processor"].setText(info["cpu"])
        self.hw["Cores"].setText(f"{info['physical_cores']} cores / {info['logical_cores']} threads")
        self.hw["Memory"].setText(fmt_size(info["ram"]))
        self.hw["Graphics"].setText(", ".join(info["gpu"]) or "Not reported")
        self.hw["Video encoders"].setText(", ".join(info["encoders"]) or "–")
        rep = hardware.last_report()
        hwe = (rep or {}).get("hw_encoders")
        self.hw["Hardware encoding"].setText(
            ", ".join(f"{k.split('_')[0].upper()} {'works' if v else 'not available'}" for k, v in hwe.items())
            if hwe else "Not tested yet (run the benchmark)")
        cur = db.setting("profile", "safe")
        self.profile.setCurrentIndex(max(0, self.profile.findData(cur)))
        self._prof_info()
        self._show_report(rep)
        self.refresh_log()

    def _prof_info(self):
        rec = hardware.recommend(hardware.last_report())
        p = self.profile.currentData()
        s = rec[p]
        txt = (f"{s['concurrency']} export(s) at a time · {s['threads_per_job'] or 'all'} CPU threads each · "
               f"hardware encoding {'on where verified' if s['hw_accel'] == 'auto' else 'off'} · "
               f"{s['analysis_workers']} analysis task(s)")
        if p == "performance" and not rec["performance_allowed"]:
            txt += "\nRun the benchmark first; until then this applies Balanced."
        cur = db.setting("profile", "safe")
        txt += f"\nCurrently active: {cur.capitalize()}."
        self.prof_info.setText(txt)

    def apply_profile(self):
        name, p = hardware.apply_profile(self.profile.currentData())
        queue.set_concurrency(int(p["concurrency"]))
        self._prof_info()
        QMessageBox.information(self, "Profile applied", f"{name.capitalize()} mode is now active.")

    def run_bench(self):
        if db.q1("SELECT COUNT(*) c FROM jobs WHERE status='running'")["c"]:
            if QMessageBox.question(self, "Exports are running", "The benchmark is more accurate when nothing "
                                    "else is running. Pause the queue and run it now?") != QMessageBox.Yes:
                return
            queue.pause()
        self.btn_bench.setEnabled(False)
        self.bench.setText("Starting…")

        def work():
            try:
                rep = hardware.benchmark(progress=self.benchMsg.emit)
                self.benchDone.emit(rep, "")
            except Exception as e:
                self.benchDone.emit(None, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _bench_done(self, rep, err):
        self.btn_bench.setEnabled(True)
        if err:
            self.bench.setText(f"Benchmark failed: {err}")
            return
        self._show_report(rep)
        self.load_static()

    def _show_report(self, rep):
        if not rep:
            self.bench.setText("No benchmark yet.")
            return
        r = rep["results"]

        def f(x, unit="× real time"):
            return f"{x} {unit}" if x else "not available"
        lines = [f"Run on {time.strftime('%d %b %Y %H:%M', time.localtime(rep['ts']))}",
                 f"Software H.264 (high quality 1080p): {f(r.get('software_h264'))}",
                 f"Software H.265: {f(r.get('software_h265'))}",
                 f"Hardware H.264 (VideoToolbox): {f(r.get('h264_videotoolbox'))}",
                 f"Hardware H.265 (VideoToolbox): {f(r.get('hevc_videotoolbox'))}",
                 f"Scene/audio analysis: {f(r.get('analysis'))}",
                 f"Speech recognition (tiny model): {f(r.get('whisper_tiny'))}",
                 f"Temp folder write speed: {f(r.get('disk_write_mb_s'), 'MB/s')}"]
        lines += rep.get("notes", [])
        self.bench.setText("\n".join(lines))

    def save_report(self):
        rep = hardware.last_report() or {"hardware": self.info or hardware.detect(), "results": None}
        path, _ = QFileDialog.getSaveFileName(self, "Save capability report",
                                              str(Path.home() / "Reels Cutter capability report.json"), "JSON (*.json)")
        if path:
            Path(path).write_text(json.dumps(rep, indent=2), encoding="utf-8")

    def refresh(self):
        self.cpu.setValue(int(hardware.cpu_percent()))
        total, avail, pct = hardware.memory()
        self.mem.setValue(int(pct))
        run = db.q1("SELECT COUNT(*) c FROM jobs WHERE status='running'")["c"]
        self.live.setText(f"{fmt_size(avail)} memory available · pressure: {hardware.memory_pressure()} · "
                          f"{run} export(s) running · power: {'battery' if hardware.on_battery() else 'mains / desktop'}"
                          + (f"\n{queue.blocked_reason}" if queue.blocked_reason else ""))

    def refresh_log(self):
        lv = self.log_level.currentIndex()
        where = ["1=1", "level IN ('error','warning')", "level='error'"][lv]
        rows = db.q(f"SELECT * FROM log WHERE {where} ORDER BY id DESC LIMIT 500")
        self.log.setPlainText("\n".join(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['ts']))}  "
                                        f"{r['level'].upper():7}  {r['message']}" for r in rows) or "Empty.")

    def export_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export log", str(Path.home() / "Reels Cutter log.txt"),
                                              "Text (*.txt)")
        if path:
            rows = db.q("SELECT * FROM log ORDER BY id")
            Path(path).write_text("\n".join(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['ts']))}  "
                                            f"{r['level'].upper():7}  {r['message']}" for r in rows), encoding="utf-8")
