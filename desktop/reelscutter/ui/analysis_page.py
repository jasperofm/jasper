"""AI Analysis: choose a level or modules, see estimates, run analysis, manage local models, search transcripts."""
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QTableView,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QGridLayout)

from .. import analysis, db, subtitles
from .tables import RowsModel
from .widgets import card, confirm, fmt_dur, label

LEVEL_NAMES = {"fast": "Fast — scenes, silence, loudness", "standard": "Standard — + transcript",
               "advanced": "Advanced — + face tracking and picture quality", "custom": "Custom — choose modules"}


class AnalysisPage(QWidget):
    openAt = Signal(int, float)
    modelDone = Signal(str, str)

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
        root.addWidget(label("AI Analysis", "h1"))
        root.addWidget(label("Everything runs on this Mac. Results are saved, so a video is never analyzed twice "
                             "for the same module.", "muted", wrap=True))

        grid = QGridLayout()
        grid.setSpacing(14)
        rc, rl = card()
        rl.addWidget(label("Run analysis", "h2"))
        self.level = QComboBox()
        for k, v in LEVEL_NAMES.items():
            self.level.addItem(v, k)
        rl.addWidget(self.level)
        self.mod_checks = {}
        for k, v in analysis.MODULES.items():
            cb = QCheckBox(v)
            self.mod_checks[k] = cb
            rl.addWidget(cb)
        self.scope = QComboBox()
        self.scope.addItems(["Videos missing these results", "Selected videos (below)", "All videos (re-analyze)"])
        rl.addWidget(self.scope)
        self.est = label("", "muted", wrap=True)
        rl.addWidget(self.est)
        r = QHBoxLayout()
        self.btn_run = QPushButton("Analyze")
        self.btn_run.setObjectName("primary")
        self.btn_stop = QPushButton("Stop")
        r.addWidget(self.btn_run)
        r.addWidget(self.btn_stop)
        rl.addLayout(r)
        self.now = label("", wrap=True)
        rl.addWidget(self.now)
        rl.addStretch()
        grid.addWidget(rc, 0, 0)

        mc, ml = card()
        ml.addWidget(label("Speech models (local)", "h2"))
        ml.addWidget(label("Needed for transcripts and captions. Models are only downloaded when you press "
                           "Install. Smaller models are faster on Intel Macs; “base” is a good balance.",
                           "muted", wrap=True))
        self.models = QTableWidget(len(subtitles.MODELS), 3)
        self.models.setHorizontalHeaderLabels(["Model", "Download", "Status"])
        self.models.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.models.verticalHeader().hide()
        self.models.setEditTriggers(QTableWidget.NoEditTriggers)
        self.models.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.models.setSelectionMode(QAbstractItemView.SingleSelection)
        self.models.setMaximumHeight(160)
        ml.addWidget(self.models)
        r2 = QHBoxLayout()
        self.btn_install = QPushButton("Install")
        self.btn_use = QPushButton("Use this model")
        self.btn_rm = QPushButton("Remove")
        for b in (self.btn_install, self.btn_use, self.btn_rm):
            r2.addWidget(b)
        ml.addLayout(r2)
        self.model_msg = label("", "muted", wrap=True)
        ml.addWidget(self.model_msg)
        ml.addStretch()
        grid.addWidget(mc, 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        sc, sl = card()
        sl.addWidget(label("Search transcripts", "h2"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Type words or a phrase, then press Enter")
        sl.addWidget(self.search)
        self.results = QListWidget()
        self.results.setMinimumHeight(140)
        self.results.setToolTip("Double-click to open the video at that moment")
        sl.addWidget(self.results)
        root.addWidget(sc)

        vc, vl = card()
        vl.addWidget(label("Videos", "h2"))
        cols = [("Video", lambda r: r["name"]), ("Length", lambda r: fmt_dur(r["duration"])),
                ("Level", lambda r: (r["analysis_level"] or ("fast" if r["has_a"] else "not analyzed")).capitalize()),
                ("Scenes", lambda r: "✓" if r["has_a"] else ""),
                ("Transcript", lambda r: "✓" if r["has_t"] else ""),
                ("Faces", lambda r: "✓" if r["has_f"] else ""),
                ("Status", lambda r: r["status"].capitalize())]
        self.vmodel = RowsModel(cols, status_col=6)
        self.vview = QTableView()
        self.vview.setModel(self.vmodel)
        self.vview.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.vview.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.vview.verticalHeader().hide()
        self.vview.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.vview.setMinimumHeight(260)
        self.vview.setShowGrid(False)
        vl.addWidget(self.vview)
        root.addWidget(vc)

        self.level.currentIndexChanged.connect(self._level_changed)
        for cb in self.mod_checks.values():
            cb.toggled.connect(self.update_estimate)
        self.scope.currentIndexChanged.connect(self.update_estimate)
        self.btn_run.clicked.connect(self.run)
        self.btn_stop.clicked.connect(analysis.runner.cancel_all)
        self.btn_install.clicked.connect(self.install)
        self.btn_use.clicked.connect(self.use_model)
        self.btn_rm.clicked.connect(self.remove_model)
        self.search.returnPressed.connect(self.do_search)
        self.results.itemDoubleClicked.connect(lambda it: self.openAt.emit(*it.data(Qt.UserRole)))
        self.modelDone.connect(self._model_done)
        self.level.setCurrentIndex(0)
        self._level_changed()
        self.refresh_models()
        self._downloading = None

    def modules(self):
        return [k for k, cb in self.mod_checks.items() if cb.isChecked()]

    def _level_changed(self, *a):
        lv = self.level.currentData()
        custom = lv == "custom"
        for k, cb in self.mod_checks.items():
            cb.setEnabled(custom)
            if not custom:
                cb.blockSignals(True)
                cb.setChecked(k in analysis.LEVELS[lv])
                cb.blockSignals(False)
        self.update_estimate()

    def target_ids(self):
        mods = set(self.modules())
        i = self.scope.currentIndex()
        if i == 1:
            return [self.vmodel.rows[x.row()]["id"] for x in self.vview.selectionModel().selectedRows()]
        rows = db.q(analysis.LIGHT + " WHERE status!='missing'")
        if i == 2:
            return [r["id"] for r in rows]
        return [r["id"] for r in rows if not mods <= analysis.done_modules(r)]

    def update_estimate(self, *a):
        ids = self.target_ids()
        mods = self.modules()
        if not ids or not mods:
            self.est.setText("Nothing to analyze with these choices.")
            return
        durs = [r["duration"] or 0 for r in db.q(
            f"SELECT duration FROM videos WHERE id IN ({','.join('?' * min(len(ids), 900))})", ids[:900])]
        durs = durs * max(1, len(ids) // max(1, len(durs)))
        secs = analysis.estimate_seconds(durs, mods)
        txt = f"{len(ids):,} video(s), {fmt_dur(sum(durs))} of footage. "
        txt += f"Estimated time on this Mac: about {fmt_dur(secs)}." if secs is not None else \
            "Run the benchmark in System Monitor to get a time estimate for this Mac."
        if "transcript" in mods and not subtitles.current_model_name():
            txt += "\nTranscripts need a speech model: install one on the right first."
        self.est.setText(txt)

    def run(self):
        mods = self.modules()
        ids = self.target_ids()
        if not ids or not mods:
            return
        if "transcript" in mods and not subtitles.current_model_name():
            QMessageBox.information(self, "Install a speech model", "Install a speech model first, or untick "
                                                                    "Transcript.")
            return
        if len(ids) > 20 and not confirm(self, "Analyze many videos", f"Analyze {len(ids):,} videos? This runs in "
                                         f"the background, one video at a time.\n\n{self.est.text()}",
                                         ok="Start", danger=False):
            return
        analysis.runner.add(ids, mods)
        self.refresh()

    # ---- models
    def refresh_models(self):
        cur = db.setting("whisper_model", "base")
        for i, (name, mb) in enumerate(subtitles.MODELS.items()):
            inst = subtitles.model_installed(name)
            self.models.setItem(i, 0, QTableWidgetItem(name + ("  (in use)" if name == cur and inst else "")))
            self.models.item(i, 0).setData(Qt.UserRole, name)
            self.models.setItem(i, 1, QTableWidgetItem(f"~{mb} MB"))
            st = "Downloading…" if getattr(self, "_downloading", None) == name else \
                "Installed" if inst else "Not installed"
            self.models.setItem(i, 2, QTableWidgetItem(st))
        if not subtitles.available():
            self.model_msg.setText("The speech-recognition component isn't included in this build.")
            for b in (self.btn_install, self.btn_use, self.btn_rm):
                b.setEnabled(False)

    def _sel_model(self):
        rows = self.models.selectionModel().selectedRows()
        return self.models.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    def install(self):
        name = self._sel_model()
        if not name or subtitles.model_installed(name) or self._downloading:
            return
        if not confirm(self, "Download speech model", f"Download the “{name}” speech model (about "
                       f"{subtitles.MODELS[name]} MB) from Hugging Face? It is stored in the app's data folder and "
                       "runs offline afterwards.", ok="Download", danger=False):
            return
        self._downloading = name
        self.refresh_models()

        def work():
            try:
                subtitles.download_model(name)
                self.modelDone.emit(name, "")
            except Exception as e:
                self.modelDone.emit(name, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _model_done(self, name, err):
        self._downloading = None
        if err:
            QMessageBox.warning(self, "Download failed", f"Couldn't download the model: {err}")
        elif not subtitles.model_installed(subtitles.current_model_name() or ""):
            pass
        if not err and not subtitles.model_installed(db.setting("whisper_model", "base")):
            db.set_setting("whisper_model", name)
        self.refresh_models()
        self.update_estimate()

    def use_model(self):
        name = self._sel_model()
        if name and subtitles.model_installed(name):
            db.set_setting("whisper_model", name)
            subtitles.unload()
            self.refresh_models()

    def remove_model(self):
        name = self._sel_model()
        if name and subtitles.model_installed(name) and confirm(self, "Remove model",
                                                                 f"Delete the “{name}” model files?"):
            subtitles.delete_model(name)
            self.refresh_models()

    # ---- search
    def do_search(self):
        self.results.clear()
        hits = analysis.search_transcripts(self.search.text())
        for vid, name, t, ctx in hits:
            it = QListWidgetItem(f"{name}  ·  {fmt_dur(t)}   …{ctx}…")
            it.setData(Qt.UserRole, (vid, float(t)))
            self.results.addItem(it)
        if not hits:
            n = db.q1("SELECT COUNT(*) c FROM videos WHERE transcript IS NOT NULL")["c"]
            self.results.addItem("No matches." if n else "No transcripts yet. Run Standard or Advanced analysis.")

    def refresh(self):
        rows = db.q(analysis.LIGHT + " ORDER BY added_at DESC LIMIT 3000")
        self.vmodel.set_rows(rows)
        r = analysis.runner
        if r.current:
            v = db.q1("SELECT name FROM videos WHERE id=?", (r.current,))
            self.now.setText(f"Analyzing {v['name'] if v else ''}: {r.status}  ·  {len(r.pending)} waiting")
        else:
            self.now.setText(f"Idle. {len(r.errors)} video(s) had errors (see System Monitor → Log)."
                             if r.errors else "Idle.")
        self.btn_stop.setEnabled(bool(r.current or r.pending))
