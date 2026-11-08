"""Video Library page: import, search, filter, thumbnails, metadata."""
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSize, Qt, Signal, QRect
from PySide6.QtGui import QColor, QPixmap, QPixmapCache, QPainter, QFont
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QListView,
                               QPushButton, QSplitter, QVBoxLayout, QWidget, QStyledItemDelegate, QStyle, QFormLayout,
                               QLabel, QMenu)

from .. import db
from . import theme
from .widgets import card, confirm, fmt_dur, fmt_size, label

DURATION_FILTERS = {"Any length": None, "Under 5 min": (0, 300), "5–20 min": (300, 1200),
                    "20–60 min": (1200, 3600), "Over 1 hour": (3600, 10 ** 9)}
RES_FILTERS = {"Any resolution": None, "4K+": (2160, 10 ** 6), "1440p": (1440, 2159), "1080p": (1080, 1439),
               "720p": (720, 1079), "Below 720p": (0, 719)}
STATUS_FILTERS = {"Any status": None, "Not processed": "new", "Queued": "queued", "Processing": "processing",
                  "Done": "done", "Failed": "failed", "Missing file": "missing"}


class VideoModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.filters = {}

    def refresh(self):
        where, args = ["1=1"], []
        f = self.filters
        if f.get("search"):
            where.append("(name LIKE ? OR tags LIKE ? OR path LIKE ?)")
            args += [f"%{f['search']}%"] * 3
        if f.get("duration"):
            where.append("duration BETWEEN ? AND ?")
            args += list(f["duration"])
        if f.get("res"):
            where.append("MIN(width,height) BETWEEN ? AND ?")
            args += list(f["res"])
        if f.get("fmt"):
            where.append("fmt=?")
            args.append(f["fmt"])
        if f.get("status"):
            st = f["status"]
            if st == "processing":
                where.append("status IN ('processing','analyzing')")
            else:
                where.append("status=?")
                args.append(st)
        if f.get("project"):
            where.append("project_id=?")
            args.append(f["project"])
        sql = ("SELECT v.id, v.path, v.name, v.size, v.duration, v.width, v.height, v.fps, v.fmt, v.thumb, v.status, "
               "v.project_id, v.added_at, v.tags, "
               "(SELECT COUNT(*) FROM jobs j WHERE j.video_id=v.id AND j.status='done') clips "
               f"FROM videos v WHERE {' AND '.join(where)} ORDER BY added_at DESC, id DESC")
        self.beginResetModel()
        self.rows = db.q(sql, args)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        if role == Qt.DisplayRole:
            return r["name"]
        if role == Qt.UserRole:
            return r
        if role == Qt.ToolTipRole:
            return f"{r['name']}\n{fmt_dur(r['duration'])} · {r['width']}×{r['height']} · {r['status']}"
        return None


class VideoDelegate(QStyledItemDelegate):
    W, H = 220, 176

    def sizeHint(self, opt, index):
        return QSize(self.W, self.H)

    def paint(self, p, opt, index):
        r = index.data(Qt.UserRole)
        rect = opt.rect.adjusted(6, 6, -6, -6)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        sel = opt.state & QStyle.State_Selected
        p.setPen(QColor(theme.ACCENT) if sel else QColor(theme.BORDER))
        p.setBrush(QColor("#1d222b" if sel else theme.PANEL))
        p.drawRoundedRect(rect, 10, 10)
        img = QRect(rect.left() + 8, rect.top() + 8, rect.width() - 16, int((rect.width() - 16) * 9 / 16))
        pm = None
        if r["thumb"]:
            pm = QPixmapCache.find(r["thumb"])
            if pm is None:
                pm = QPixmap(r["thumb"])
                if not pm.isNull():
                    QPixmapCache.insert(r["thumb"], pm)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#0b0d11"))
        p.drawRoundedRect(img, 6, 6)
        if pm is not None and not pm.isNull():
            scaled = pm.scaled(img.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x = img.left() + (img.width() - scaled.width()) // 2
            y = img.top() + (img.height() - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)
        # duration badge
        badge = fmt_dur(r["duration"])
        f = QFont(p.font())
        f.setPointSize(9)
        f.setBold(True)
        p.setFont(f)
        bw = p.fontMetrics().horizontalAdvance(badge) + 10
        b = QRect(img.right() - bw - 4, img.bottom() - 20, bw, 16)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(b, 4, 4)
        p.setPen(QColor("white"))
        p.drawText(b, Qt.AlignCenter, badge)
        # status dot + name
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.STATUS_COLORS.get(r["status"], theme.MUTED)))
        p.drawEllipse(rect.left() + 10, img.bottom() + 13, 8, 8)
        f.setBold(False)
        f.setPointSize(11)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT))
        name_r = QRect(rect.left() + 24, img.bottom() + 6, rect.width() - 32, 20)
        p.drawText(name_r, Qt.AlignVCenter | Qt.AlignLeft,
                   p.fontMetrics().elidedText(r["name"], Qt.ElideMiddle, name_r.width()))
        f.setPointSize(9)
        p.setFont(f)
        p.setPen(QColor(theme.MUTED))
        sub = f"{r['width']}×{r['height']} · {r['fmt'].upper()} · {r['clips']} clips"
        p.drawText(QRect(rect.left() + 10, img.bottom() + 26, rect.width() - 20, 16), Qt.AlignLeft, sub)
        p.restore()


class LibraryPage(QWidget):
    openInEditor = Signal(int)
    createClips = Signal(list)
    importRequested = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        top = QHBoxLayout()
        top.addWidget(label("Video Library", "h1"))
        top.addStretch()
        self.btn_files = QPushButton("Import videos…")
        self.btn_files.setToolTip("Add video files (⌘I). You can also drag files or folders onto the window.")
        self.btn_folder = QPushButton("Import folder…")
        self.btn_folder.setToolTip("Add every video inside a folder and its sub-folders (⇧⌘I)")
        self.btn_clip = QPushButton("Create clips")
        self.btn_clip.setObjectName("primary")
        self.btn_clip.setToolTip("Automatically cut clips from the selected videos (or all shown videos)")
        for b in (self.btn_files, self.btn_folder, self.btn_clip):
            top.addWidget(b)
        root.addLayout(top)

        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by file name…  (⌘F)")
        self.search.setClearButtonEnabled(True)
        self.f_dur = QComboBox()
        self.f_dur.addItems(DURATION_FILTERS)
        self.f_res = QComboBox()
        self.f_res.addItems(RES_FILTERS)
        self.f_fmt = QComboBox()
        self.f_status = QComboBox()
        self.f_status.addItems(STATUS_FILTERS)
        self.f_project = QComboBox()
        bar.addWidget(self.search, 3)
        for w in (self.f_dur, self.f_res, self.f_fmt, self.f_status, self.f_project):
            bar.addWidget(w, 1)
        root.addLayout(bar)

        split = QSplitter()
        self.model = VideoModel()
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(VideoDelegate())
        self.view.setViewMode(QListView.IconMode)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setUniformItemSizes(True)
        self.view.setMovement(QListView.Static)
        self.view.setSpacing(2)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        split.addWidget(self.view)

        side, sl = card()
        side.setMinimumWidth(270)
        side.setMaximumWidth(340)
        self.preview = QLabel()
        self.preview.setFixedHeight(150)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("background:#0b0d11;border-radius:8px;")
        sl.addWidget(self.preview)
        self.title = label("Select a video", "h2", wrap=True)
        sl.addWidget(self.title)
        self.meta = QFormLayout()
        self.meta.setLabelAlignment(Qt.AlignLeft)
        self.meta_labels = {}
        for k in ("Duration", "Resolution", "Frame rate", "Video codec", "Audio", "Format", "Size", "Status",
                  "Clips made", "Project", "Location"):
            v = label("–", wrap=True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.meta_labels[k] = v
            kl = label(k, "muted")
            self.meta.addRow(kl, v)
        sl.addLayout(self.meta)
        sl.addStretch()
        self.btn_edit = QPushButton("Open in editor")
        self.btn_reveal = QPushButton("Show in Finder")
        self.btn_remove = QPushButton("Remove from library")
        self.btn_remove.setObjectName("danger")
        self.btn_remove.setToolTip("Removes the video from Reels Cutter only. The original file is never deleted.")
        for b in (self.btn_edit, self.btn_reveal, self.btn_remove):
            sl.addWidget(b)
        split.addWidget(side)
        split.setStretchFactor(0, 1)
        root.addWidget(split, 1)
        self.count = label("", "muted")
        root.addWidget(self.count)

        self.search.textChanged.connect(self.apply_filters)
        for c in (self.f_dur, self.f_res, self.f_fmt, self.f_status, self.f_project):
            c.currentIndexChanged.connect(self.apply_filters)
        self.view.selectionModel().selectionChanged.connect(self.show_details)
        self.view.doubleClicked.connect(lambda i: self.openInEditor.emit(i.data(Qt.UserRole)["id"]))
        self.view.customContextMenuRequested.connect(self.context_menu)
        self.btn_files.clicked.connect(self.import_files)
        self.btn_folder.clicked.connect(self.import_folder)
        self.btn_clip.clicked.connect(lambda: self.createClips.emit(self.target_ids()))
        self.btn_edit.clicked.connect(self._edit_selected)
        self.btn_reveal.clicked.connect(self._reveal_selected)
        self.btn_remove.clicked.connect(self.remove_selected)
        self.reload_filters()
        self.apply_filters()
        self.show_details()

    # ---- data
    def reload_filters(self):
        cur_fmt, cur_proj = self.f_fmt.currentText(), self.f_project.currentData()
        self.f_fmt.blockSignals(True)
        self.f_project.blockSignals(True)
        self.f_fmt.clear()
        self.f_fmt.addItem("Any format", None)
        for r in db.q("SELECT DISTINCT fmt FROM videos ORDER BY fmt"):
            self.f_fmt.addItem(r["fmt"].upper(), r["fmt"])
        i = self.f_fmt.findText(cur_fmt)
        self.f_fmt.setCurrentIndex(max(0, i))
        self.f_project.clear()
        self.f_project.addItem("All projects", None)
        for r in db.projects():
            self.f_project.addItem(r["name"], r["id"])
        i = self.f_project.findData(cur_proj)
        self.f_project.setCurrentIndex(max(0, i))
        self.f_fmt.blockSignals(False)
        self.f_project.blockSignals(False)

    def apply_filters(self):
        self.model.filters = {
            "search": self.search.text().strip(),
            "duration": DURATION_FILTERS[self.f_dur.currentText()],
            "res": RES_FILTERS[self.f_res.currentText()],
            "fmt": self.f_fmt.currentData(),
            "status": STATUS_FILTERS[self.f_status.currentText()],
            "project": self.f_project.currentData(),
        }
        self.refresh()

    def refresh(self):
        sel = set(self.selected_ids())
        self.model.refresh()
        sm = self.view.selectionModel()
        for row, r in enumerate(self.model.rows):
            if r["id"] in sel:
                sm.select(self.model.index(row), sm.Select)
        total = db.q1("SELECT COUNT(*) c FROM videos")["c"]
        self.count.setText(f"Showing {len(self.model.rows)} of {total} videos")
        self.show_details()

    def selected_ids(self):
        return [i.data(Qt.UserRole)["id"] for i in self.view.selectionModel().selectedIndexes()]

    def target_ids(self):
        ids = self.selected_ids()
        return ids or [r["id"] for r in self.model.rows]

    def show_details(self, *a):
        ids = self.selected_ids()
        enabled = len(ids) == 1
        for b in (self.btn_edit, self.btn_reveal):
            b.setEnabled(enabled)
        self.btn_remove.setEnabled(bool(ids))
        self.btn_clip.setText(f"Create clips ({len(ids)})" if ids else "Create clips (all shown)")
        if not enabled:
            self.title.setText(f"{len(ids)} videos selected" if ids else "Select a video")
            self.preview.clear()
            for v in self.meta_labels.values():
                v.setText("–")
            return
        r = db.q1("SELECT v.*, (SELECT COUNT(*) FROM jobs j WHERE j.video_id=v.id AND j.status='done') clips "
                  "FROM videos v WHERE id=?", (ids[0],))
        if not r:
            return
        self.title.setText(r["name"])
        pm = QPixmap(r["thumb"]) if r["thumb"] else QPixmap()
        self.preview.setPixmap(pm.scaled(self.preview.width() or 250, 150, Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation) if not pm.isNull() else QPixmap())
        vals = {
            "Duration": fmt_dur(r["duration"]), "Resolution": f"{r['width']} × {r['height']}",
            "Frame rate": f"{r['fps']:.2f} fps" if r["fps"] else "–", "Video codec": r["vcodec"] or "–",
            "Audio": r["acodec"] or "No audio", "Format": (r["fmt"] or "").upper(), "Size": fmt_size(r["size"]),
            "Status": r["status"].capitalize() + (f" — {r['error']}" if r["error"] else ""),
            "Clips made": str(r["clips"]), "Project": db.project_name(r["project_id"]), "Location": r["path"],
        }
        for k, v in vals.items():
            self.meta_labels[k].setText(v)

    # ---- actions
    def import_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Import videos", str(Path.home()),
                                                "Videos (*.mp4 *.mov *.m4v *.mkv *.avi *.webm *.mts *.m2ts *.ts "
                                                "*.wmv *.flv *.mpg *.mpeg *.3gp);;All files (*)")
        if files:
            self.importRequested.emit(files)

    def import_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Import a folder of videos", str(Path.home()))
        if d:
            self.importRequested.emit([d])

    def _edit_selected(self):
        ids = self.selected_ids()
        if ids:
            self.openInEditor.emit(ids[0])

    def _reveal_selected(self):
        ids = self.selected_ids()
        if ids:
            r = db.q1("SELECT path FROM videos WHERE id=?", (ids[0],))
            reveal(r["path"])

    def remove_selected(self):
        ids = self.selected_ids()
        if not ids:
            return
        busy = db.q1(f"SELECT COUNT(*) c FROM jobs WHERE status='running' AND video_id IN "
                     f"({','.join('?' * len(ids))})", ids)["c"]
        if busy:
            confirm(self, "Videos are being processed",
                    "Some of these videos are being exported right now. Cancel their jobs in the Queue first.",
                    ok="OK", danger=False)
            return
        if not confirm(self, "Remove from library",
                       f"Remove {len(ids)} video(s) from Reels Cutter?\n\nThe original files stay where they are, "
                       "and clips you already exported are kept. Queued jobs for these videos are canceled."):
            return
        ph = ",".join("?" * len(ids))
        db.run(f"UPDATE jobs SET status='canceled' WHERE status='queued' AND video_id IN ({ph})", ids)
        db.run(f"DELETE FROM batch_videos WHERE video_id IN ({ph})", ids)
        db.run(f"DELETE FROM segments WHERE video_id IN ({ph})", ids)
        for r in db.q(f"SELECT thumb FROM videos WHERE id IN ({ph})", ids):
            if r["thumb"]:
                Path(r["thumb"]).unlink(missing_ok=True)
        db.run(f"DELETE FROM videos WHERE id IN ({ph})", ids)
        self.refresh()

    def context_menu(self, pos):
        if not self.selected_ids():
            return
        m = QMenu(self)
        m.addAction("Open in editor", self._edit_selected)
        m.addAction("Create clips…", lambda: self.createClips.emit(self.selected_ids()))
        m.addAction("Show in Finder", self._reveal_selected)
        m.addSeparator()
        m.addAction("Remove from library…", self.remove_selected)
        m.exec(self.view.viewport().mapToGlobal(pos))


def reveal(path):
    path = str(path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    elif sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", path])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


def open_path(path):
    path = str(path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("win"):
        import os
        os.startfile(path)  # noqa
    else:
        subprocess.Popen(["xdg-open", path])
