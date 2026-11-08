"""Settings: naming, automation, quality checks, scheduling, learning, cloud AI and app info."""
import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QHBoxLayout,
                               QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from .. import db, export, paths
from .widgets import card, confirm, label


class SettingsPage(QWidget):
    showOnboarding = Signal()
    showAbout = Signal()

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
        root.addWidget(label("Settings", "h1"))
        root.addWidget(label("Changes are saved immediately.", "muted"))
        grid = QGridLayout()
        grid.setSpacing(14)

        # naming
        nc, nl = card()
        nl.addWidget(label("File naming", "h2"))
        f = QFormLayout()
        self.folder_t = QLineEdit(db.setting("folder_template"))
        self.name_t = QLineEdit(db.setting("name_template"))
        f.addRow("Folder", self.folder_t)
        f.addRow("File name", self.name_t)
        nl.addLayout(f)
        nl.addWidget(label("Fields: {project} {video} {n} {start} {end} {length} {preset}. "
                           "Existing files are never overwritten; a number is added instead.", "muted", wrap=True))
        self.preview = label("", "muted", wrap=True)
        nl.addWidget(self.preview)
        grid.addWidget(nc, 0, 0)

        # automation
        ac, al = card()
        al.addWidget(label("Automatic pipeline", "h2"))
        self.review = QCheckBox("Require manual review before clips count as ready")
        self.review.setToolTip("Off (default): verified clips go straight to Ready to Export. "
                               "On: they wait in Awaiting Review.")
        self.retry = QCheckBox("Retry failed exports automatically")
        al.addWidget(self.review)
        al.addWidget(self.retry)
        f2 = QFormLayout()
        self.retry_limit = QSpinBox()
        self.retry_limit.setRange(0, 5)
        self.repairs = QSpinBox()
        self.repairs.setRange(0, 2)
        self.repairs.setToolTip("Alternative encoder settings tried when an export fails or a file is broken: "
                                "1 = software encoder, 2 = also a safe simple H.264 setup")
        f2.addRow("Automatic retries per clip", self.retry_limit)
        f2.addRow("Repair strategies", self.repairs)
        al.addLayout(f2)
        al.addStretch()
        grid.addWidget(ac, 0, 1)

        # quality control
        qc, ql = card()
        ql.addWidget(label("Quality checks", "h2"))
        self.qc_on = QCheckBox("Check every clip after export")
        ql.addWidget(self.qc_on)
        f3 = QFormLayout()
        self.qc_spins = {}
        for key, name in (("qc_max_black_pct", "Max black frames"), ("qc_max_frozen_pct", "Max frozen picture"),
                          ("qc_max_silence_pct", "Max silence")):
            s = QSpinBox()
            s.setRange(0, 100)
            s.setSuffix(" %")
            self.qc_spins[key] = s
            f3.addRow(name, s)
        ql.addLayout(f3)
        ql.addWidget(label("Always checked: file opens and decodes, codec, resolution, duration, audio present "
                           "and in sync. Clips that fail are moved to _Quarantine.", "muted", wrap=True))
        grid.addWidget(qc, 1, 0)

        # schedule
        sc, sl = card()
        sl.addWidget(label("Scheduling and power", "h2"))
        self.sched = QCheckBox("Only start new exports during these hours")
        sl.addWidget(self.sched)
        h = QHBoxLayout()
        self.s_start = QSpinBox()
        self.s_start.setRange(0, 23)
        self.s_start.setSuffix(":00")
        self.s_end = QSpinBox()
        self.s_end.setRange(0, 23)
        self.s_end.setSuffix(":00")
        h.addWidget(label("From", "muted"))
        h.addWidget(self.s_start)
        h.addWidget(label("to", "muted"))
        h.addWidget(self.s_end)
        h.addStretch()
        sl.addLayout(h)
        self.battery = QCheckBox("Pause new exports while on battery power")
        sl.addWidget(self.battery)
        sl.addWidget(label("Exports already running always finish. The queue resumes by itself when the "
                           "conditions are met, also after restarting the app.", "muted", wrap=True))
        sl.addStretch()
        grid.addWidget(sc, 1, 1)

        # learning
        lc, ll = card()
        ll.addWidget(label("Learning from your choices", "h2"))
        self.learn = QCheckBox("Remember which clips I approve or reject")
        ll.addWidget(self.learn)
        self.learn_info = label("", "muted", wrap=True)
        ll.addWidget(self.learn_info)
        r = QHBoxLayout()
        self.btn_learn_reset = QPushButton("Reset learned preferences")
        r.addWidget(self.btn_learn_reset)
        r.addStretch()
        ll.addLayout(r)
        grid.addWidget(lc, 2, 0)

        # cloud
        cc, cl = card()
        cl.addWidget(label("Cloud AI (optional)", "h2"))
        cl.addWidget(label("Off by default. When on, short transcript excerpts (never video) may be sent to "
                           "Anthropic's Claude API to help rank candidate clips. Requires your own API key.",
                           "muted", wrap=True))
        self.cloud = QCheckBox("Allow cloud-assisted analysis")
        cl.addWidget(self.cloud)
        self.key = QLineEdit(db.setting("anthropic_api_key"))
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("Anthropic API key")
        cl.addWidget(self.key)
        cl.addStretch()
        grid.addWidget(cc, 2, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        bc, bl = card(QHBoxLayout)
        self.btn_onb = QPushButton("Show welcome guide")
        self.btn_about = QPushButton("About Reels Cutter")
        self.btn_reset = QPushButton("Restore default settings")
        self.btn_reset.setObjectName("danger")
        for b in (self.btn_onb, self.btn_about):
            bl.addWidget(b)
        bl.addStretch()
        bl.addWidget(self.btn_reset)
        root.addWidget(bc)
        root.addStretch()

        self.load()
        self.folder_t.textChanged.connect(lambda t: self._set("folder_template", t))
        self.name_t.textChanged.connect(lambda t: self._set("name_template", t))
        self.review.toggled.connect(lambda v: self._set("review_required", int(v)))
        self.retry.toggled.connect(lambda v: self._set("auto_retry", int(v)))
        self.retry_limit.valueChanged.connect(lambda v: self._set("auto_retry_limit", v))
        self.repairs.valueChanged.connect(lambda v: self._set("max_repairs", v))
        self.qc_on.toggled.connect(lambda v: self._set("qc_enabled", int(v)))
        for k, s in self.qc_spins.items():
            s.valueChanged.connect(lambda v, k=k: self._set(k, v))
        self.sched.toggled.connect(lambda v: self._set("schedule_enabled", int(v)))
        self.s_start.valueChanged.connect(lambda v: self._set("schedule_start", v))
        self.s_end.valueChanged.connect(lambda v: self._set("schedule_end", v))
        self.battery.toggled.connect(lambda v: self._set("pause_on_battery", int(v)))
        self.learn.toggled.connect(lambda v: self._set("learning_enabled", int(v)))
        self.btn_learn_reset.clicked.connect(self.reset_learning)
        self.cloud.toggled.connect(self._cloud)
        self.key.editingFinished.connect(lambda: db.set_setting("anthropic_api_key", self.key.text().strip()))
        self.btn_onb.clicked.connect(self.showOnboarding.emit)
        self.btn_about.clicked.connect(self.showAbout.emit)
        self.btn_reset.clicked.connect(self.reset)

    def load(self):
        from .. import qc
        ws = [self.folder_t, self.name_t, self.review, self.retry, self.retry_limit, self.repairs, self.qc_on,
              self.sched, self.s_start, self.s_end, self.battery, self.learn, self.cloud] + list(self.qc_spins.values())
        for w in ws:
            w.blockSignals(True)
        self.folder_t.setText(db.setting("folder_template"))
        self.name_t.setText(db.setting("name_template"))
        self.review.setChecked(db.setting("review_required") == "1")
        self.retry.setChecked(db.setting("auto_retry") == "1")
        self.retry_limit.setValue(int(db.setting("auto_retry_limit")))
        self.repairs.setValue(int(db.setting("max_repairs")))
        self.qc_on.setChecked(db.setting("qc_enabled", "1") == "1")
        for k, s in self.qc_spins.items():
            s.setValue(int(float(db.setting(k, qc.DEFAULTS[k]))))
        self.sched.setChecked(db.setting("schedule_enabled") == "1")
        self.s_start.setValue(int(db.setting("schedule_start")))
        self.s_end.setValue(int(db.setting("schedule_end")))
        self.battery.setChecked(db.setting("pause_on_battery") == "1")
        self.learn.setChecked(db.setting("learning_enabled") == "1")
        self.cloud.setChecked(db.setting("cloud_ai_enabled") == "1")
        self.key.setEnabled(self.cloud.isChecked())
        for w in ws:
            w.blockSignals(False)
        self._preview()
        self._learn_info()

    def _set(self, k, v):
        db.set_setting(k, v)
        if k.endswith("template"):
            self._preview()
        from ..jobs import queue
        queue.wake.set()

    def _preview(self):
        p = export.output_path(db.setting("output_dir"), db.setting("folder_template"), db.setting("name_template"),
                               project="My Project", video_name="Interview.mp4", n=3, start=754, end=784,
                               preset_name="Reels 9:16 1080p")
        self.preview.setText(f"Example: {p}")

    def _learn_info(self):
        from .. import learning
        self.learn_info.setText(learning.describe())

    def reset_learning(self):
        if confirm(self, "Reset learning", "Forget all approve/reject history used for recommendations?",
                   ok="Reset"):
            db.run("DELETE FROM feedback")
            self._learn_info()

    def _cloud(self, on):
        if on and not confirm(self, "Allow cloud AI?", "Short transcript excerpts may be sent to Anthropic's API "
                              "when you use cloud-assisted ranking. Video and audio are never uploaded. Allow?",
                              ok="Allow", danger=False):
            self.cloud.blockSignals(True)
            self.cloud.setChecked(False)
            self.cloud.blockSignals(False)
            return
        db.set_setting("cloud_ai_enabled", int(on))
        self.key.setEnabled(on)

    def reset(self):
        if not confirm(self, "Restore defaults", "Restore all settings to their defaults? Your library, clips, "
                       "presets and output folder are kept.", ok="Restore"):
            return
        keep = {"output_dir", "onboarded", "hardware_report", "temp_dir", "anthropic_api_key", "last_generator"}
        db.run(f"DELETE FROM settings WHERE key NOT IN ({','.join('?' * len(keep))})", list(keep))
        self.load()
