"""Export presets: built-in and custom (resolution, frame rate, codec, quality, aspect, framing, audio)."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QInputDialog,
                               QListWidget, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget, QLineEdit)

from .. import db, export, hardware
from .widgets import card, confirm, label

BUILTIN = set(db.BUILTIN_PRESETS)


class PresetsPage(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 8, 0, 0)
        root.setSpacing(14)
        lc, ll = card()
        lc.setMaximumWidth(280)
        ll.addWidget(label("Presets", "h2"))
        self.list = QListWidget()
        ll.addWidget(self.list, 1)
        r = QHBoxLayout()
        self.btn_new = QPushButton("New")
        self.btn_dup = QPushButton("Duplicate")
        self.btn_del = QPushButton("Delete")
        self.btn_del.setObjectName("danger")
        for b in (self.btn_new, self.btn_dup, self.btn_del):
            r.addWidget(b)
        ll.addLayout(r)
        root.addWidget(lc)

        fc, fl = card()
        self.title = label("", "h2")
        fl.addWidget(self.title)
        self.note = label("", "muted", wrap=True)
        fl.addWidget(self.note)
        f = QFormLayout()
        self.aspect = QComboBox()
        for k in export.ASPECTS:
            self.aspect.addItem("Original shape" if k == "original" else k, k)
        self.res = QComboBox()
        for k, v in export.RES_LABELS.items():
            self.res.addItem(v, k)
        self.fps = QComboBox()
        for k in export.FPS_CHOICES:
            self.fps.addItem("Same as source" if k == "source" else f"{k} fps", k)
        self.codec = QComboBox()
        self.codec.addItem("H.264 (plays everywhere)", "h264")
        self.codec.addItem("H.265 / HEVC (smaller files)", "h265")
        self.mode = QComboBox()
        self.mode.addItem("Constant quality (recommended)", "crf")
        self.mode.addItem("Target bitrate", "bitrate")
        self.crf = QSpinBox()
        self.crf.setRange(10, 35)
        self.crf.setToolTip("Lower = better quality and bigger files. 16–20 is visually excellent.")
        self.br = QDoubleSpinBox()
        self.br.setRange(1, 200)
        self.br.setSuffix(" Mbit/s")
        self.abr = QComboBox()
        for k in (96, 128, 160, 192, 256, 320):
            self.abr.addItem(f"{k} kbit/s", k)
        self.fit = QComboBox()
        for k, v in export.FITS.items():
            self.fit.addItem(v, k)
        self.fade_in = QDoubleSpinBox()
        self.fade_in.setRange(0, 5)
        self.fade_in.setSuffix(" s")
        self.fade_out = QDoubleSpinBox()
        self.fade_out.setRange(0, 5)
        self.fade_out.setSuffix(" s")
        self.norm = QCheckBox("Normalize loudness (−14 LUFS, good for phones)")
        self.norm.setToolTip("Off keeps the original audio level untouched")
        rows = [("Aspect ratio", self.aspect), ("Resolution", self.res), ("Frame rate", self.fps),
                ("Video codec", self.codec), ("Quality mode", self.mode), ("Quality (CRF)", self.crf),
                ("Bitrate", self.br), ("Audio bitrate", self.abr), ("Framing", self.fit), ("Fade in", self.fade_in),
                ("Fade out", self.fade_out), ("", self.norm)]
        for name, w in rows:
            f.addRow(name, w)
        fl.addLayout(f)
        self.size = label("", "muted", wrap=True)
        fl.addWidget(self.size)
        fl.addStretch()
        r2 = QHBoxLayout()
        self.btn_default = QPushButton("Use as default")
        self.btn_save = QPushButton("Save")
        self.btn_save.setObjectName("primary")
        r2.addWidget(self.btn_default)
        r2.addStretch()
        r2.addWidget(self.btn_save)
        fl.addLayout(r2)
        root.addWidget(fc, 1)

        self.list.currentTextChanged.connect(self.load)
        self.btn_new.clicked.connect(self.new)
        self.btn_dup.clicked.connect(self.duplicate)
        self.btn_del.clicked.connect(self.delete)
        self.btn_save.clicked.connect(self.save)
        self.btn_default.clicked.connect(self.make_default)
        self.mode.currentIndexChanged.connect(self._mode)
        for w in (self.aspect, self.res, self.codec):
            w.currentIndexChanged.connect(self._size)
        self.reload()

    def reload(self, select=None):
        cur = select or (self.list.currentItem().text() if self.list.currentItem() else None)
        self.list.blockSignals(True)
        self.list.clear()
        d = db.setting("default_preset")
        for name in db.presets():
            self.list.addItem(name)
            it = self.list.item(self.list.count() - 1)
            if name == d:
                it.setText(name)
                it.setToolTip("Default preset")
                f = it.font()
                f.setBold(True)
                it.setFont(f)
        self.list.blockSignals(False)
        items = self.list.findItems(cur or d, Qt.MatchExactly)
        self.list.setCurrentItem(items[0] if items else self.list.item(0))
        self.load(self.list.currentItem().text())

    def load(self, name):
        if not name:
            return
        p = dict(export.DEFAULT_PRESET, **db.presets().get(name, {}))
        self.current = name
        builtin = name in BUILTIN
        self.title.setText(name)
        self.note.setText("Built-in preset: Save creates a copy you can change." if builtin else "Custom preset.")
        self.btn_del.setEnabled(not builtin)

        def sel(combo, val):
            i = combo.findData(val)
            combo.setCurrentIndex(max(0, i))
        sel(self.aspect, p["aspect"])
        sel(self.res, p["resolution"])
        sel(self.fps, str(p["fps"]))
        sel(self.codec, p["codec"])
        sel(self.mode, p["quality_mode"])
        self.crf.setValue(int(p["crf"]))
        self.br.setValue(float(p["bitrate_mbps"]))
        sel(self.abr, int(p["audio_kbps"]))
        sel(self.fit, p["fit"])
        self.fade_in.setValue(float(p["fade_in"]))
        self.fade_out.setValue(float(p["fade_out"]))
        self.norm.setChecked(bool(p["normalize_audio"]))
        self._mode()
        self._size()

    def gather(self):
        base = dict(export.DEFAULT_PRESET, **db.presets().get(self.current, {}))
        base.update(aspect=self.aspect.currentData(), resolution=self.res.currentData(), fps=self.fps.currentData(),
                    codec=self.codec.currentData(), quality_mode=self.mode.currentData(), crf=self.crf.value(),
                    bitrate_mbps=self.br.value(), audio_kbps=self.abr.currentData(), fit=self.fit.currentData(),
                    fade_in=self.fade_in.value(), fade_out=self.fade_out.value(),
                    normalize_audio=self.norm.isChecked())
        return base

    def _mode(self):
        crf = self.mode.currentData() == "crf"
        self.crf.setEnabled(crf)
        self.br.setEnabled(not crf)

    def _size(self):
        p = self.gather()
        w, h = export.target_size(p, 1920, 1080)
        hw = hardware.last_report() or {}
        hwok = (hw.get("hw_encoders") or {}).get("h264_videotoolbox" if p["codec"] == "h264" else
                                                  "hevc_videotoolbox")
        self.size.setText(f"From a 1920×1080 source this exports {w}×{h}. "
                          + ("Hardware encoding was verified on this Mac and is used in Balanced/Performance mode "
                             "for bitrate presets." if hwok else
                             "Uses the high-quality software encoder (hardware encoding not verified on this Mac)."))

    def save(self):
        name = self.current
        if name in BUILTIN:
            name, ok = QInputDialog.getText(self, "Save as new preset", "Name for your copy:", text=f"{name} (custom)")
            if not ok or not name.strip():
                return
            name = name.strip()
            if name in BUILTIN:
                QMessageBox.warning(self, "Name taken", "That's a built-in preset name.")
                return
        db.save_preset(name, self.gather())
        self.reload(name)

    def new(self):
        name, ok = QInputDialog.getText(self, "New preset", "Preset name:")
        if ok and name.strip() and name.strip() not in BUILTIN:
            db.save_preset(name.strip(), dict(export.DEFAULT_PRESET))
            self.reload(name.strip())

    def duplicate(self):
        name, ok = QInputDialog.getText(self, "Duplicate preset", "Name:", text=f"{self.current} copy")
        if ok and name.strip() and name.strip() not in BUILTIN:
            db.save_preset(name.strip(), self.gather())
            self.reload(name.strip())

    def delete(self):
        if self.current in BUILTIN:
            return
        if confirm(self, "Delete preset", f"Delete the preset “{self.current}”? Clips already exported are kept."):
            db.delete_preset(self.current)
            if db.setting("default_preset") == self.current:
                db.set_setting("default_preset", "Reels 9:16 1080p")
            self.reload()

    def make_default(self):
        db.set_setting("default_preset", self.current)
        self.reload(self.current)
