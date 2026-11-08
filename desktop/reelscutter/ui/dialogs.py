"""Welcome guide (with hardware detection and storage setup), About and keyboard shortcuts."""
import platform
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget, QTableWidget, QTableWidgetItem, QHeaderView)

from .. import db, hardware, paths
from ..media import NO_WINDOW
from .widgets import fmt_size, label

SHORTCUTS = [
    ("⌘1 … ⌘9, ⌘0", "Switch pages"), ("⌘I", "Import videos"), ("⇧⌘I", "Import a folder"),
    ("⌘F", "Search the library"), ("⌘G", "Clip Generator"), ("⌘P", "Pause / resume all processing"),
    ("Space", "Play / pause (Editor, Review)"), ("I / O", "Set IN / OUT point"), ("Enter", "Save IN→OUT as a segment"),
    ("← / →", "Previous / next frame"), ("⇧← / ⇧→", "Back / forward 5 seconds"), ("P", "Preview the selection"),
    ("F", "Fullscreen video (Esc to leave)"), ("M", "Mute"), ("= / -", "Zoom the timeline"),
    ("A / R / E", "Approve / Reject / Needs edit (Review)"), ("⌫", "Delete selected segment"),
]


class _Detect(QThread):
    done = Signal(object)

    def run(self):
        try:
            info = hardware.detect()
            info["hw_test"] = hardware.test_hw_encoders()
        except Exception as e:
            info = {"error": str(e)}
        self.done.emit(info)


class Onboarding(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to Reels Cutter")
        self.setMinimumSize(640, 500)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 20)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        p1 = QWidget()
        l1 = QVBoxLayout(p1)
        l1.addWidget(label("Welcome to Reels Cutter", "h1"))
        l1.addWidget(label(
            "Reels Cutter finds the best moments in your long videos and turns them into finished short clips, "
            "automatically and on this Mac.\n\n"
            "1.  Import videos (drag them onto the window, or ⌘I).\n"
            "2.  Open Clip Generator, pick lengths and a format, press Start.\n"
            "3.  Reels Cutter analyzes, chooses moments, frames, captions, exports and quality-checks every clip.\n"
            "4.  Find finished clips in Clip Review Studio, or straight in your output folder.\n\n"
            "Your original videos are never changed or deleted. Use footage you own or have the rights to.",
            wrap=True))
        l1.addStretch()
        self.stack.addWidget(p1)

        p2 = QWidget()
        l2 = QVBoxLayout(p2)
        l2.addWidget(label("Your Mac", "h1"))
        self.hw_text = label("Detecting hardware and testing video encoders…", wrap=True)
        self.hw_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l2.addWidget(self.hw_text)
        l2.addWidget(label("Reels Cutter starts in Safe mode: one export at a time, gentle on memory. After you've "
                           "run the benchmark in System Monitor it can recommend Balanced or Performance.",
                           "muted", wrap=True))
        l2.addStretch()
        self.stack.addWidget(p2)

        p3 = QWidget()
        l3 = QVBoxLayout(p3)
        l3.addWidget(label("Where should clips go?", "h1"))
        l3.addWidget(label("With a 256 GB drive, saving exports to an external SSD keeps your Mac's storage free. "
                           "You can change this any time in Storage Manager.", "muted", wrap=True))
        self.drives = QTableWidget(0, 3)
        self.drives.setHorizontalHeaderLabels(["Drive", "Type", "Free"])
        self.drives.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.drives.verticalHeader().hide()
        self.drives.setEditTriggers(QTableWidget.NoEditTriggers)
        self.drives.setMaximumHeight(150)
        l3.addWidget(self.drives)
        row = QHBoxLayout()
        self.out_lbl = label("", wrap=True)
        b = QPushButton("Choose folder…")
        b.clicked.connect(self.choose_out)
        row.addWidget(self.out_lbl, 1)
        row.addWidget(b)
        l3.addLayout(row)
        self.space_warn = label("", wrap=True)
        self.space_warn.setStyleSheet("color:#f5b84b;")
        l3.addWidget(self.space_warn)
        l3.addStretch()
        self.stack.addWidget(p3)

        p4 = QWidget()
        l4 = QVBoxLayout(p4)
        l4.addWidget(label("You're ready", "h1"))
        l4.addWidget(label("Optional next steps:\n\n"
                           "•  AI Analysis → Models: install a speech model (asks before downloading) for "
                           "transcripts, sentence-aware cuts and captions.\n"
                           "•  System Monitor → Run benchmark: measures this Mac to recommend faster settings and "
                           "give time estimates.\n\n"
                           "Hover over any control for a tip. Settings → Show welcome guide brings this back.",
                           wrap=True))
        l4.addStretch()
        self.stack.addWidget(p4)

        nav = QHBoxLayout()
        self.step = label("", "muted")
        nav.addWidget(self.step)
        nav.addStretch()
        self.btn_back = QPushButton("Back")
        self.btn_next = QPushButton("Next")
        self.btn_next.setObjectName("primary")
        nav.addWidget(self.btn_back)
        nav.addWidget(self.btn_next)
        root.addLayout(nav)
        self.btn_back.clicked.connect(lambda: self.go(-1))
        self.btn_next.clicked.connect(lambda: self.go(1))
        self.det = _Detect()
        self.det.done.connect(self._detected)
        self.det.start()
        self._fill_drives()
        self.go(0)

    def go(self, d):
        i = self.stack.currentIndex() + d
        if i >= self.stack.count():
            db.set_setting("onboarded", "1")
            self.accept()
            return
        self.stack.setCurrentIndex(i)
        self.btn_back.setEnabled(i > 0)
        self.btn_next.setText("Start using Reels Cutter" if i == self.stack.count() - 1 else "Next")
        self.step.setText(f"Step {i + 1} of {self.stack.count()}")

    def _detected(self, info):
        if "error" in info:
            self.hw_text.setText(f"Couldn't read all hardware details ({info['error']}). Safe mode will be used.")
            return
        hw = info.get("hw_test", {})
        lines = [f"System: {info['os']} ({info['arch']}" + (", Apple Silicon)" if info["apple_silicon"] else ", Intel)"),
                 f"Processor: {info['cpu']} — {info['physical_cores']} cores / {info['logical_cores']} threads",
                 f"Memory: {fmt_size(info['ram'])}",
                 f"Graphics: {', '.join(info['gpu']) or 'not reported'}",
                 "Hardware H.264 encoding: " + ("works" if hw.get("h264_videotoolbox") else "not available — "
                                                "high-quality software encoding will be used"),
                 "Hardware H.265 encoding: " + ("works" if hw.get("hevc_videotoolbox") else "not available")]
        self.hw_text.setText("\n".join(lines))
        rep = hardware.last_report() or {}
        rep.setdefault("ts", 0)
        rep["hardware"] = {k: v for k, v in info.items() if k != "hw_test"}
        rep["hw_encoders"] = hw
        rep.setdefault("results", {})
        import json
        db.set_setting("hardware_report", json.dumps(rep))
        if not db.setting("profile_set_by_user"):
            hardware.apply_profile("safe")

    def _fill_drives(self):
        vols = hardware.volumes()
        self.drives.setRowCount(len(vols))
        for i, v in enumerate(vols):
            for c, t in enumerate((f"{v['name']}  ({v['path']})", "External" if v["external"] else "Internal",
                                   fmt_size(v["free"]))):
                self.drives.setItem(i, c, QTableWidgetItem(t))
        self._out_changed()

    def choose_out(self):
        d = QFileDialog.getExistingDirectory(self, "Folder for exported clips", db.setting("output_dir"))
        if d:
            db.set_setting("output_dir", str(Path(d) / "Reels Cutter") if Path(d).name != "Reels Cutter" else d)
            self._out_changed()

    def _out_changed(self):
        out = db.setting("output_dir")
        self.out_lbl.setText(f"Clips will be saved in:\n{out}")
        free = hardware.free_gb(paths.data_dir())
        reserve = float(db.setting("internal_reserve_gb"))
        if free is not None and free < reserve + 5:
            self.space_warn.setText(f"Your internal drive has {free:.0f} GB free. New exports pause when it drops "
                                    f"below {reserve:.0f} GB (change this in Storage Manager).")
        else:
            self.space_warn.setText("")


class About(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Reels Cutter")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        lay.addWidget(label(paths.APP_NAME, "h1"))
        lay.addWidget(label(f"Version {paths.VERSION}", "muted"))
        try:
            ff = subprocess.run([paths.ffmpeg(), "-version"], capture_output=True, text=True, timeout=10,
                                **NO_WINDOW).stdout.splitlines()[0]
        except Exception:
            ff = "FFmpeg not found"
        import PySide6
        txt = (f"{ff}\nQt {PySide6.__version__} · Python {platform.python_version()} ({platform.machine()})\n"
               f"Data folder: {paths.data_dir()}\n\n"
               "Local video processing with FFmpeg. Optional speech recognition with faster-whisper "
               "(OpenAI Whisper models), face detection with OpenCV.\nFFmpeg is licensed under the LGPL/GPL; "
               "Qt for Python under the LGPL.")
        t = label(txt, "muted", wrap=True)
        t.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(t)
        lay.addWidget(label("Keyboard shortcuts", "h2"))
        tbl = QTableWidget(len(SHORTCUTS), 2)
        tbl.horizontalHeader().hide()
        tbl.verticalHeader().hide()
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        for i, (k, v) in enumerate(SHORTCUTS):
            tbl.setItem(i, 0, QTableWidgetItem(k))
            tbl.setItem(i, 1, QTableWidgetItem(v))
        tbl.setMinimumHeight(260)
        lay.addWidget(tbl)
        b = QPushButton("Close")
        b.clicked.connect(self.accept)
        lay.addWidget(b, alignment=Qt.AlignRight)
