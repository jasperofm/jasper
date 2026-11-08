"""Storage Manager: drives, locations (internal vs external), cache limits and cleanup, missing-file relinking."""
import os
import shutil
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QMessageBox, QPushButton,
                               QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QDoubleSpinBox,
                               QLineEdit, QGridLayout)

from .. import db, hardware, media, paths
from ..jobs import queue
from .library import open_path
from .widgets import card, confirm, fmt_size, label


def dir_size(p):
    total = 0
    for root, dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def trim_cache(folder, limit_bytes, keep=()):
    """Delete oldest files until the folder is under the limit. Never touches files listed in keep."""
    files = []
    for root, dirs, fs in os.walk(folder):
        for f in fs:
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
                files.append((st.st_atime, st.st_size, p))
            except OSError:
                pass
    total = sum(s for _, s, _ in files)
    removed = 0
    for _, s, p in sorted(files):
        if total <= limit_bytes:
            break
        if p in keep:
            continue
        try:
            os.unlink(p)
            total -= s
            removed += s
        except OSError:
            pass
    return removed


class StoragePage(QWidget):
    sizesReady = Signal(dict)

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
        top = QHBoxLayout()
        top.addWidget(label("Storage Manager", "h1"))
        top.addStretch()
        self.btn_refresh = QPushButton("Refresh")
        top.addWidget(self.btn_refresh)
        root.addLayout(top)
        root.addWidget(label("Tip for a 256 GB Mac: keep source videos, exports and temporary files on an external "
                             "SSD, and only the app, database and small thumbnails on the internal drive.",
                             "muted", wrap=True))

        dc, dl = card()
        dl.addWidget(label("Drives", "h2"))
        self.drives = QTableWidget(0, 4)
        self.drives.setHorizontalHeaderLabels(["Drive", "Type", "Free", "Total"])
        self.drives.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.drives.verticalHeader().hide()
        self.drives.setMaximumHeight(170)
        self.drives.setEditTriggers(QTableWidget.NoEditTriggers)
        dl.addWidget(self.drives)
        root.addWidget(dc)

        lc, ll = card()
        ll.addWidget(label("Locations", "h2"))
        g = QGridLayout()
        self.loc = {}
        rows = [("output_dir", "Exported clips", "Where finished clips are saved. An external SSD is a good choice."),
                ("temp_dir", "Temporary files", "Scratch space for analysis and captions. Leave empty for the "
                                                "internal default.")]
        for i, (key, name, tip) in enumerate(rows):
            e = QLineEdit()
            e.setReadOnly(True)
            e.setToolTip(tip)
            b = QPushButton("Change…")
            o = QPushButton("Open")
            g.addWidget(label(name, "muted"), i, 0)
            g.addWidget(e, i, 1)
            g.addWidget(b, i, 2)
            g.addWidget(o, i, 3)
            self.loc[key] = e
            b.clicked.connect(lambda _=False, k=key: self.choose(k))
            o.clicked.connect(lambda _=False, k=key: self.open_loc(k))
        g.addWidget(label("App data (database, thumbnails, models)", "muted"), 2, 0)
        e = QLineEdit(str(paths.data_dir()))
        e.setReadOnly(True)
        g.addWidget(e, 2, 1)
        o = QPushButton("Open")
        o.clicked.connect(lambda: open_path(paths.data_dir()))
        g.addWidget(o, 2, 3)
        g.setColumnStretch(1, 1)
        ll.addLayout(g)
        root.addWidget(lc)

        cc, cl = card()
        cl.addWidget(label("Caches and safety limits", "h2"))
        f = QFormLayout()
        self.limits = {}
        for key, name, lo, hi, tip in [
            ("thumb_cache_gb", "Thumbnail cache limit", 0.1, 50, "Oldest clip thumbnails are removed above this"),
            ("temp_cache_gb", "Temporary files limit", 1, 500, "Temporary files above this are cleaned up "
                                                               "(never ones an active job is using)"),
            ("internal_reserve_gb", "Keep free on internal drive", 0, 500, "New exports pause below this"),
            ("min_free_gb", "Keep free on export drive", 0, 500, "New exports pause below this")]:
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setDecimals(1)
            s.setSuffix(" GB")
            s.setValue(float(db.setting(key)))
            s.setToolTip(tip)
            s.valueChanged.connect(lambda v, k=key: db.set_setting(k, v))
            self.limits[key] = s
            f.addRow(name, s)
        cl.addLayout(f)
        self.usage = label("Calculating…", "muted", wrap=True)
        cl.addWidget(self.usage)
        r = QHBoxLayout()
        self.btn_clean = QPushButton("Clean temporary files")
        self.btn_thumbs = QPushButton("Trim thumbnail cache")
        r.addWidget(self.btn_clean)
        r.addWidget(self.btn_thumbs)
        r.addStretch()
        cl.addLayout(r)
        root.addWidget(cc)

        mc, ml = card()
        ml.addWidget(label("Missing source videos", "h2"))
        ml.addWidget(label("Videos whose file was moved, renamed or is on a disconnected drive. Reconnect the "
                           "drive and press Check again, or relink by choosing the folder where the files are now. "
                           "Files are matched by name and verified by fingerprint.", "muted", wrap=True))
        self.missing = QTableWidget(0, 2)
        self.missing.setHorizontalHeaderLabels(["Video", "Last known location"])
        self.missing.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.missing.verticalHeader().hide()
        self.missing.setEditTriggers(QTableWidget.NoEditTriggers)
        self.missing.setMinimumHeight(140)
        ml.addWidget(self.missing)
        r2 = QHBoxLayout()
        self.btn_check = QPushButton("Check again")
        self.btn_relink = QPushButton("Relink from folder…")
        self.btn_relink.setObjectName("primary")
        r2.addWidget(self.btn_check)
        r2.addWidget(self.btn_relink)
        r2.addStretch()
        ml.addLayout(r2)
        root.addWidget(mc)
        root.addStretch()

        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_clean.clicked.connect(self.clean_temp)
        self.btn_thumbs.clicked.connect(self.trim_thumbs)
        self.btn_check.clicked.connect(self.check_missing)
        self.btn_relink.clicked.connect(self.relink)
        self.sizesReady.connect(self._sizes)
        self._loaded = False

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()

    def refresh(self):
        vols = hardware.volumes()
        self.drives.setRowCount(len(vols))
        for i, v in enumerate(vols):
            for c, t in enumerate((f"{v['name']}  ({v['path']})", "External" if v["external"] else "Internal",
                                   fmt_size(v["free"]), fmt_size(v["total"]))):
                self.drives.setItem(i, c, QTableWidgetItem(t))
        self.loc["output_dir"].setText(db.setting("output_dir"))
        self.loc["temp_dir"].setText(db.setting("temp_dir") or f"(default) {paths.data_dir() / 'tmp'}")
        self.usage.setText("Calculating…")

        def work():
            d = paths.data_dir()
            self.sizesReady.emit(dict(thumbs=dir_size(d / "thumbs"), tmp=dir_size(hardware.temp_dir()),
                                      models=dir_size(d / "models"), db=sum(
                                          (d / n).stat().st_size for n in os.listdir(d) if n.startswith("library")),
                                      backups=dir_size(d / "backups")))
        threading.Thread(target=work, daemon=True).start()
        self.load_missing()

    def _sizes(self, s):
        self.usage.setText(f"Thumbnails {fmt_size(s['thumbs'])} · temporary {fmt_size(s['tmp'])} · AI models "
                           f"{fmt_size(s['models'])} · database {fmt_size(s['db'])} · backups {fmt_size(s['backups'])}")

    def choose(self, key):
        d = QFileDialog.getExistingDirectory(self, "Choose a folder", db.setting(key) or str(Path.home()))
        if not d:
            return
        if not os.access(d, os.W_OK):
            QMessageBox.warning(self, "Can't write there", "Reels Cutter can't write to that folder.")
            return
        if key == "temp_dir" and db.q1("SELECT COUNT(*) c FROM jobs WHERE status='running'")["c"]:
            QMessageBox.information(self, "Busy", "Change the temporary folder when no exports are running.")
            return
        db.set_setting(key, d)
        self.refresh()

    def open_loc(self, key):
        p = Path(db.setting(key) or (paths.data_dir() / "tmp"))
        p.mkdir(parents=True, exist_ok=True)
        open_path(p)

    def clean_temp(self):
        if queue.running or db.q1("SELECT COUNT(*) c FROM batch_videos WHERE state='planning'")["c"]:
            QMessageBox.information(self, "Busy", "Temporary files are in use. Pause the queue and wait for the "
                                                  "running exports to finish, then try again.")
            return
        n = 0
        for tmp in {paths.data_dir() / "tmp", hardware.temp_dir()}:
            for f in tmp.glob("*"):
                try:
                    n += dir_size(f) if f.is_dir() else f.stat().st_size
                    shutil.rmtree(f) if f.is_dir() else f.unlink()
                except OSError:
                    pass
        QMessageBox.information(self, "Cleaned", f"Freed {fmt_size(n)}.")
        self.refresh()

    def trim_thumbs(self):
        keep = {r["thumb"] for r in db.q("SELECT thumb FROM videos WHERE thumb IS NOT NULL")}
        n = trim_cache(paths.data_dir() / "thumbs", float(db.setting("thumb_cache_gb")) * 1e9 * 0.8, keep)
        QMessageBox.information(self, "Thumbnail cache", f"Freed {fmt_size(n)}. Library thumbnails are kept.")
        self.refresh()

    def load_missing(self):
        rows = db.q("SELECT id,name,path FROM videos WHERE status='missing' ORDER BY name LIMIT 2000")
        self.missing.setRowCount(len(rows))
        for i, r in enumerate(rows):
            it = QTableWidgetItem(r["name"])
            it.setData(Qt.UserRole, r["id"])
            self.missing.setItem(i, 0, it)
            self.missing.setItem(i, 1, QTableWidgetItem(r["path"]))
        self.btn_relink.setEnabled(bool(rows))

    def check_missing(self):
        n_missing = n_back = 0
        for r in db.q("SELECT id,path,status FROM videos"):
            exists = Path(r["path"]).exists()
            if not exists and r["status"] != "missing":
                db.run("UPDATE videos SET status='missing' WHERE id=?", (r["id"],))
                n_missing += 1
            elif exists and r["status"] == "missing":
                queue._update_video_status(r["id"])
                db.run("UPDATE videos SET status='new' WHERE id=? AND status='missing'", (r["id"],))
                n_back += 1
        queue.wake.set()
        self.load_missing()
        QMessageBox.information(self, "Checked", f"{n_back} video(s) found again, {n_missing} newly missing.")

    def relink(self):
        d = QFileDialog.getExistingDirectory(self, "Folder that contains the moved videos")
        if not d:
            return
        missing = {r["name"]: r for r in db.q("SELECT id,name,path,fingerprint FROM videos WHERE status='missing'")}
        fixed, mismatched = 0, 0
        for root, dirs, files in os.walk(d):
            for f in files:
                r = missing.get(f)
                if not r:
                    continue
                p = Path(root) / f
                try:
                    if r["fingerprint"] and media.fingerprint(p) != r["fingerprint"]:
                        mismatched += 1
                        continue
                    db.run("UPDATE videos SET path=?, status='new' WHERE id=?", (str(p.resolve()), r["id"]))
                    queue._update_video_status(r["id"])
                    fixed += 1
                    missing.pop(f)
                except Exception:
                    pass
        queue.wake.set()
        self.load_missing()
        msg = f"Relinked {fixed} video(s)."
        if mismatched:
            msg += f" {mismatched} file(s) had the right name but different content, so they were not linked."
        QMessageBox.information(self, "Relink", msg)
