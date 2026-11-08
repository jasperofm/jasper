"""Clip Generator: configure the automatic pipeline once, then run it on any number of videos.

Settings are saved as reusable production profiles (selection settings + export preset + options).
"""
import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QHBoxLayout,
                               QInputDialog, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox,
                               QVBoxLayout, QWidget)

from .. import db, selection, subtitles
from ..jobs import queue
from .widgets import card, confirm, fmt_dur, label

DURATIONS = [("15 s", 15), ("30 s", 30), ("60 s", 60), ("90 s", 90), ("2 min", 120), ("3 min", 180),
             ("5 min", 300)]


def _spin(lo, hi, val, suffix="", dec=False, step=1):
    w = QDoubleSpinBox() if dec else QSpinBox()
    w.setRange(lo, hi)
    if dec:
        w.setDecimals(1)
        w.setSingleStep(step)
    w.setValue(val)
    if suffix:
        w.setSuffix(suffix)
    return w


class GeneratorPage(QWidget):
    started = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self.video_ids = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(label("Clip Generator", "h1"))
        top.addStretch()
        top.addWidget(label("Profile", "muted"))
        self.profile = QComboBox()
        self.profile.setMinimumWidth(200)
        self.profile.setToolTip("Saved production profiles: all settings on this page")
        self.btn_save_prof = QPushButton("Save profile…")
        self.btn_del_prof = QPushButton("Delete")
        for w in (self.profile, self.btn_save_prof, self.btn_del_prof):
            top.addWidget(w)
        outer.addLayout(top)
        self.target = label("", "muted", wrap=True)
        outer.addWidget(self.target)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        body.setObjectName("page")
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 8, 0)
        grid.setSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # --- selection
        c1, l1 = card()
        l1.addWidget(label("What to cut", "h2"))
        f1 = QFormLayout()
        self.mode = QComboBox()
        for m in selection.MODES:
            self.mode.addItem(selection.MODE_LABELS[m], m)
        self.mode.setToolTip("How moments are chosen. Hybrid uses your saved timeline segments first, then "
                             "loud/active moments and scene changes.")
        f1.addRow("Selection strategy", self.mode)
        self.per_video = _spin(1, 200, 5)
        self.per_video.setToolTip("Target number of clips from each source video")
        f1.addRow("Clips per video", self.per_video)
        l1.addLayout(f1)
        l1.addWidget(label("Clip lengths (ticked lengths are mixed for variety)", "muted"))
        drow = QGridLayout()
        self.dur_checks = []
        for i, (txt, secs) in enumerate(DURATIONS):
            cb = QCheckBox(txt)
            cb.setProperty("secs", secs)
            self.dur_checks.append(cb)
            drow.addWidget(cb, i // 4, i % 4)
        self.custom_on = QCheckBox("Custom")
        self.custom = _spin(3, 3600, 45, " s")
        drow.addWidget(self.custom_on, 1, 3)
        drow.addWidget(self.custom, 2, 0, 1, 2)
        l1.addLayout(drow)
        f1b = QFormLayout()
        self.min_len = _spin(1, 3600, 10, " s", True)
        self.max_len = _spin(1, 3600, 90, " s", True)
        self.spacing = _spin(0, 600, 5, " s", True)
        self.spacing.setToolTip("Minimum gap between two clips from the same video (prevents overlap)")
        f1b.addRow("Minimum length", self.min_len)
        f1b.addRow("Maximum length", self.max_len)
        f1b.addRow("Minimum spacing", self.spacing)
        l1.addLayout(f1b)
        grid.addWidget(c1, 0, 0)

        # --- smart rules
        c2, l2 = card()
        l2.addWidget(label("Smart rules", "h2"))
        f2 = QFormLayout()
        self.skip_intro = _spin(0, 3600, 0, " s", True)
        self.skip_outro = _spin(0, 3600, 0, " s", True)
        self.skip_intro.setToolTip("Never use the first N seconds (intros, countdowns)")
        self.skip_outro.setToolTip("Never use the last N seconds (outros, end screens)")
        f2.addRow("Skip beginning", self.skip_intro)
        f2.addRow("Skip ending", self.skip_outro)
        self.pre_roll = _spin(0, 10, 0, " s", True, 0.1)
        self.post_roll = _spin(0, 10, 0, " s", True, 0.1)
        self.pre_roll.setToolTip("Extra time kept before each clip's chosen start")
        self.post_roll.setToolTip("Extra time kept after each clip's chosen end")
        f2.addRow("Pre-roll", self.pre_roll)
        f2.addRow("Post-roll", self.post_roll)
        self.min_score = _spin(0, 100, 0)
        self.min_score.setToolTip("Smart and Speech modes skip candidates below this score (0 = keep all). "
                                  "Scores rank moments in your footage; they don't predict views.")
        f2.addRow("Minimum score", self.min_score)
        self.keywords = QLineEdit(db.setting("score_keywords") or "")
        self.keywords.setPlaceholderText("e.g. money, secret, how to")
        self.keywords.setToolTip("Transcript words that raise a candidate's score (needs transcripts)")
        self.exclusions = QLineEdit(db.setting("score_exclusions") or "")
        self.exclusions.setPlaceholderText("words that rule a clip out")
        f2.addRow("Boost keywords", self.keywords)
        f2.addRow("Exclude words", self.exclusions)
        l2.addLayout(f2)
        self.avoid_used = QCheckBox("Don't reuse sections already exported from the same video")
        self.avoid_silence = QCheckBox("Avoid long silences")
        self.avoid_black = QCheckBox("Avoid black / empty footage")
        self.speech_only = QCheckBox("Only parts with speech or sound")
        self.snap = QCheckBox("End clips at natural pauses (don't cut words in half)")
        for w in (self.avoid_used, self.avoid_silence, self.avoid_black, self.speech_only, self.snap):
            l2.addWidget(w)
        l2.addStretch()
        grid.addWidget(c2, 0, 1)

        # --- output
        c3, l3 = card()
        l3.addWidget(label("Output", "h2"))
        f3 = QFormLayout()
        self.preset = QComboBox()
        self.preset.setToolTip("Format, size, frame rate and quality. Edit presets in Export Manager → Presets.")
        f3.addRow("Export preset", self.preset)
        self.reframe = QComboBox()
        self.reframe.addItem("Smart: follow faces", "auto")
        self.reframe.addItem("Fixed centre", "manual")
        self.reframe.setToolTip("Only applies to presets that crop to fill (e.g. 9:16 from a wide video)")
        f3.addRow("Reframing", self.reframe)
        self.sens = _spin(0, 1, 0.5, "", True, 0.1)
        self.sens.setToolTip("0 = calm, slow camera; 1 = follows the face closely")
        f3.addRow("Tracking sensitivity", self.sens)
        self.captions = QComboBox()
        self.captions.addItem("No captions", "none")
        self.captions.addItem("Burn in captions", "burn")
        self.captions.addItem("Burn in + save .srt/.vtt files", "both")
        self.captions.addItem("Only save .srt/.vtt files", "files")
        f3.addRow("Captions", self.captions)
        self.cap_style = QComboBox()
        self.cap_style.addItems(subtitles.STYLES)
        f3.addRow("Caption style", self.cap_style)
        self.cap_pos = QComboBox()
        self.cap_pos.addItems(list(subtitles.POSITIONS))
        f3.addRow("Caption position", self.cap_pos)
        self.project = QComboBox()
        self.project.setEditable(True)
        self.project.setToolTip("Clips are filed under this project. Type a new name to create one.")
        f3.addRow("Project", self.project)
        l3.addLayout(f3)
        self.cap_note = label("", "muted", wrap=True)
        l3.addWidget(self.cap_note)
        grid.addWidget(c3, 1, 0)

        # --- run
        c4, l4 = card()
        l4.addWidget(label("Run", "h2"))
        self.estimate = label("", wrap=True)
        l4.addWidget(self.estimate)
        l4.addWidget(label("The pipeline runs by itself: analysis → selection → framing → captions → export → "
                           "quality check → automatic repair. Clips that fail quality checks are moved to a "
                           "_Quarantine folder, never mixed with good ones. Choosing different sections does not "
                           "make clips unique in any platform's eyes; it just avoids repeating yourself.",
                           "muted", wrap=True))
        l4.addStretch()
        self.btn_run = QPushButton("Start generating")
        self.btn_run.setObjectName("primary")
        self.btn_run.setMinimumHeight(40)
        l4.addWidget(self.btn_run)
        grid.addWidget(c4, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        self.btn_run.clicked.connect(self.run)
        self.btn_save_prof.clicked.connect(self.save_profile)
        self.btn_del_prof.clicked.connect(self.delete_profile)
        self.profile.activated.connect(lambda i: self.load_profile(self.profile.currentText()))
        for w in (self.per_video, self.min_len, self.max_len):
            w.valueChanged.connect(self.update_estimate)
        for cb in self.dur_checks + [self.custom_on]:
            cb.toggled.connect(self.update_estimate)
        self.custom.valueChanged.connect(self.update_estimate)
        self.captions.currentIndexChanged.connect(self.update_estimate)
        self.reload()
        self.apply(self.default_settings())

    # ---- data
    def default_settings(self):
        s = selection.SelectionSettings()
        return dict(selection=s.to_dict(), preset=db.setting("default_preset"), reframe="auto", sensitivity=0.5,
                    captions="none", caption_style="classic", caption_position="bottom", project="Default")

    def reload(self):
        cur = self.preset.currentText()
        self.preset.clear()
        self.preset.addItems(list(db.presets()))
        self.preset.setCurrentIndex(max(0, self.preset.findText(cur or db.setting("default_preset"))))
        curp = self.project.currentText()
        self.project.clear()
        self.project.addItems([r["name"] for r in db.projects()])
        if curp:
            self.project.setCurrentText(curp)
        curprof = self.profile.currentText()
        self.profile.clear()
        self.profile.addItem("(current settings)")
        for r in db.q("SELECT name FROM profiles ORDER BY name"):
            self.profile.addItem(r["name"])
        i = self.profile.findText(curprof)
        self.profile.setCurrentIndex(max(0, i))
        self.update_estimate()

    def set_targets(self, video_ids):
        self.video_ids = list(video_ids)
        self.update_estimate()

    def gather(self):
        durs = [cb.property("secs") for cb in self.dur_checks if cb.isChecked()]
        if self.custom_on.isChecked():
            durs.append(self.custom.value())
        s = selection.SelectionSettings(
            mode=self.mode.currentData(), clips_per_video=self.per_video.value(),
            durations=[float(d) for d in durs] or [30.0], min_len=self.min_len.value(), max_len=self.max_len.value(),
            min_spacing=self.spacing.value(), skip_intro=self.skip_intro.value(), skip_outro=self.skip_outro.value(),
            avoid_silence=self.avoid_silence.isChecked(), avoid_black=self.avoid_black.isChecked(),
            avoid_used=self.avoid_used.isChecked(), speech_only=self.speech_only.isChecked(),
            snap_to_pauses=self.snap.isChecked(), min_score=self.min_score.value(),
            pre_roll=self.pre_roll.value(), post_roll=self.post_roll.value())
        return dict(selection=s.to_dict(), preset=self.preset.currentText(), reframe=self.reframe.currentData(),
                    sensitivity=self.sens.value(), captions=self.captions.currentData(),
                    caption_style=self.cap_style.currentText(), caption_position=self.cap_pos.currentText(),
                    project=self.project.currentText().strip() or "Default")

    def apply(self, d):
        s = selection.SelectionSettings.from_dict(d.get("selection"))
        self.mode.setCurrentIndex(max(0, self.mode.findData(s.mode)))
        self.per_video.setValue(int(s.clips_per_video))
        presets = {secs for _, secs in DURATIONS}
        for cb in self.dur_checks:
            cb.setChecked(cb.property("secs") in [int(x) for x in s.durations])
        custom = [x for x in s.durations if int(x) not in presets]
        self.custom_on.setChecked(bool(custom))
        if custom:
            self.custom.setValue(int(custom[0]))
        self.min_len.setValue(s.min_len)
        self.max_len.setValue(s.max_len)
        self.spacing.setValue(s.min_spacing)
        self.skip_intro.setValue(s.skip_intro)
        self.skip_outro.setValue(s.skip_outro)
        self.avoid_used.setChecked(s.avoid_used)
        self.avoid_silence.setChecked(s.avoid_silence)
        self.avoid_black.setChecked(s.avoid_black)
        self.speech_only.setChecked(s.speech_only)
        self.snap.setChecked(s.snap_to_pauses)
        self.min_score.setValue(int(s.min_score or 0))
        self.pre_roll.setValue(s.pre_roll or 0)
        self.post_roll.setValue(s.post_roll or 0)
        i = self.preset.findText(d.get("preset") or "")
        if i >= 0:
            self.preset.setCurrentIndex(i)
        self.reframe.setCurrentIndex(max(0, self.reframe.findData(d.get("reframe", "auto"))))
        self.sens.setValue(float(d.get("sensitivity", 0.5)))
        self.captions.setCurrentIndex(max(0, self.captions.findData(d.get("captions", "none"))))
        self.cap_style.setCurrentText(d.get("caption_style", "classic"))
        self.cap_pos.setCurrentText(d.get("caption_position", "bottom"))
        self.project.setCurrentText(d.get("project", "Default"))
        self.update_estimate()

    def load_profile(self, name):
        r = db.q1("SELECT data FROM profiles WHERE name=?", (name,))
        if r:
            self.apply(json.loads(r["data"]))

    def save_profile(self):
        name, ok = QInputDialog.getText(self, "Save production profile", "Profile name:",
                                        text=self.profile.currentText() if self.profile.currentIndex() else "")
        if ok and name.strip():
            db.run("INSERT INTO profiles(name,data) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET data=excluded.data",
                   (name.strip(), json.dumps(self.gather())))
            self.reload()
            self.profile.setCurrentText(name.strip())

    def delete_profile(self):
        if self.profile.currentIndex() <= 0:
            return
        name = self.profile.currentText()
        if confirm(self, "Delete profile", f"Delete the production profile “{name}”?"):
            db.run("DELETE FROM profiles WHERE name=?", (name,))
            self.reload()

    def update_estimate(self, *a):
        n = len(self.video_ids)
        if n:
            rows = db.q(f"SELECT name,duration FROM videos WHERE id IN ({','.join('?' * n)})", self.video_ids) \
                if n < 900 else db.q("SELECT name,duration FROM videos")
            total = sum(r["duration"] or 0 for r in rows)
            self.target.setText(f"Source: {n:,} video(s), {fmt_dur(total)} of footage. "
                                "Change the selection in the Video Library.")
        else:
            self.target.setText("Source: no videos chosen yet. Select videos in the Video Library and press "
                                "“Create clips”, or press Start to use every video in the library.")
        g = self.gather()
        s = g["selection"]
        avg = sum(s["durations"]) / len(s["durations"])
        k = n or db.q1("SELECT COUNT(*) c FROM videos")["c"]
        self.estimate.setText(f"Up to {k * s['clips_per_video']:,} clips of about {avg:.0f}s each "
                              f"(≈ {fmt_dur(k * s['clips_per_video'] * avg)} of output). Fewer if a video "
                              f"doesn't have enough usable footage.")
        caps = g["captions"] != "none"
        if caps and not subtitles.available():
            self.cap_note.setText("Captions need the speech-recognition component, which isn't installed.")
        elif caps and not subtitles.current_model_name():
            self.cap_note.setText("Captions need a speech model. Install one in AI Analysis → Models first.")
        else:
            self.cap_note.setText("")

    def build_preset(self, g):
        p = dict(db.presets()[g["preset"]])
        p["reframe_mode"] = g["reframe"]
        p["track_sensitivity"] = g["sensitivity"]
        p["subtitles"] = g["captions"] in ("burn", "both")
        p["sidecar_captions"] = g["captions"] in ("both", "files")
        p["caption_style"] = g["caption_style"]
        p["caption_position"] = g["caption_position"]
        return p

    def run(self):
        db.set_setting("score_keywords", self.keywords.text().strip())
        db.set_setting("score_exclusions", self.exclusions.text().strip())
        g = self.gather()
        s = selection.SelectionSettings.from_dict(g["selection"])
        if s.min_len > s.max_len:
            QMessageBox.warning(self, "Check lengths", "Minimum length is longer than the maximum length.")
            return
        ids = self.video_ids or [r["id"] for r in db.q("SELECT id FROM videos WHERE status!='missing'")]
        if not ids:
            QMessageBox.information(self, "No videos", "Import some videos into the library first.")
            return
        if g["captions"] != "none" and not subtitles.current_model_name():
            if not confirm(self, "No speech model", "Captions need a speech model, and none is installed. "
                                                    "Continue without captions?", ok="Continue", danger=False):
                return
            g["captions"] = "none"
        preset = self.build_preset(g)
        queue.create_batch(ids, s, g["preset"], preset, project=g["project"],
                           name=f"{self.profile.currentText() if self.profile.currentIndex() else 'Batch'}")
        db.set_setting("last_generator", json.dumps(g))
        self.started.emit()
