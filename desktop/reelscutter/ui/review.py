"""Clip Review Studio: every generated clip in a gallery, with optional review, QC results and delivery."""
import csv
import html
import json
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPixmap, QPixmapCache, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog,
                               QLineEdit, QListView, QMenu, QMessageBox, QPushButton, QSplitter, QStyle,
                               QStyledItemDelegate, QTabBar, QVBoxLayout, QWidget)

from .. import db
from ..jobs import queue
from . import theme
from .library import open_path, reveal
from .player import Player
from .widgets import card, confirm, fmt_dur, fmt_size, label

CATS = [("ready", "Ready to Export"), ("awaiting", "Awaiting Review"), ("approved", "Approved"),
        ("needs_edit", "Needs Editing"), ("rejected", "Rejected"), ("failed_qc", "Failed Quality Check"),
        ("all", "All clips")]
REVIEW_COLORS = {"ready": theme.GOOD, "approved": theme.GOOD, "awaiting": theme.WARN, "needs_edit": theme.INFO,
                 "rejected": theme.MUTED, "failed_qc": theme.BAD}
PAGE = 600


class ClipModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        if role == Qt.UserRole:
            return r
        if role == Qt.DisplayRole:
            return Path(r["out_path"] or "").name
        if role == Qt.ToolTipRole:
            return f"{Path(r['out_path'] or '').name}\nfrom {r['vname']} at {fmt_dur(r['start'])}"
        return None


class ClipDelegate(QStyledItemDelegate):
    W, H = 168, 300

    def sizeHint(self, opt, index):
        return QSize(self.W, self.H)

    def paint(self, p, opt, index):
        r = index.data(Qt.UserRole)
        rect = opt.rect.adjusted(5, 5, -5, -5)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        sel = opt.state & QStyle.State_Selected
        p.setPen(QColor(theme.ACCENT) if sel else QColor(theme.BORDER))
        p.setBrush(QColor(theme.PANEL2 if sel else theme.PANEL))
        p.drawRoundedRect(rect, 10, 10)
        img = QRect(rect.left() + 8, rect.top() + 8, rect.width() - 16, rect.height() - 80)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#0b0d11"))
        p.drawRoundedRect(img, 6, 6)
        if r["thumb"]:
            pm = QPixmapCache.find(r["thumb"])
            if pm is None:
                pm = QPixmap(r["thumb"])
                if not pm.isNull():
                    QPixmapCache.insert(r["thumb"], pm)
            if pm is not None and not pm.isNull():
                s = pm.scaled(img.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                p.drawPixmap(img.left() + (img.width() - s.width()) // 2, img.top() + (img.height() - s.height()) // 2,
                             s)
        f = QFont(p.font())
        f.setPointSize(9)
        f.setBold(True)
        p.setFont(f)
        badge = f"{r['end'] - r['start']:.0f}s"
        bw = p.fontMetrics().horizontalAdvance(badge) + 10
        b = QRect(img.right() - bw - 4, img.bottom() - 20, bw, 16)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(b, 4, 4)
        p.setPen(QColor("white"))
        p.drawText(b, Qt.AlignCenter, badge)
        col = QColor(REVIEW_COLORS.get(r["review"], theme.MUTED))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(rect.left() + 10, img.bottom() + 12, 8, 8)
        f.setBold(False)
        f.setPointSize(10)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT))
        nr = QRect(rect.left() + 24, img.bottom() + 5, rect.width() - 30, 20)
        name = r["title"] or Path(r["out_path"] or "").stem
        p.drawText(nr, Qt.AlignVCenter | Qt.AlignLeft, p.fontMetrics().elidedText(name, Qt.ElideMiddle, nr.width()))
        f.setPointSize(9)
        p.setFont(f)
        p.setPen(QColor(theme.MUTED))
        sub = p.fontMetrics().elidedText(f"{r['vname'] or ''} · {fmt_dur(r['start'])}", Qt.ElideMiddle,
                                         rect.width() - 20)
        p.drawText(QRect(rect.left() + 10, img.bottom() + 26, rect.width() - 20, 16), Qt.AlignLeft, sub)
        tags = (r["tags"] or "").strip()
        if tags:
            p.drawText(QRect(rect.left() + 10, img.bottom() + 44, rect.width() - 20, 16), Qt.AlignLeft,
                       p.fontMetrics().elidedText("# " + tags, Qt.ElideRight, rect.width() - 20))
        p.restore()


class ReviewPage(QWidget):
    reEdit = Signal(int, float, float)

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(label("Clip Review Studio", "h1"))
        top.addStretch()
        self.btn_deliver = QPushButton("Copy to folder…")
        self.btn_deliver.setToolTip("Copy the selected clips (or every clip in this tab) to another folder, "
                                    "e.g. an external drive")
        self.btn_report = QPushButton("Export report…")
        self.btn_report.setToolTip("Save a CSV or HTML report of the clips in this tab")
        self.btn_folder = QPushButton("Open output folder")
        for b in (self.btn_folder, self.btn_report, self.btn_deliver):
            top.addWidget(b)
        root.addLayout(top)

        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        for _, name in CATS:
            self.tabs.addTab(name)
        root.addWidget(self.tabs)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search clip names, source videos or tags…")
        self.search.setClearButtonEnabled(True)
        self.f_project = QComboBox()
        self.sort = QComboBox()
        self.sort.addItems(["Newest first", "Oldest first", "Longest first", "Shortest first", "Source video"])
        bar.addWidget(self.search, 3)
        bar.addWidget(self.f_project, 1)
        bar.addWidget(self.sort, 1)
        root.addLayout(bar)

        split = QSplitter()
        self.model = ClipModel()
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(ClipDelegate())
        self.view.setViewMode(QListView.IconMode)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setUniformItemSizes(True)
        self.view.setMovement(QListView.Static)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        split.addWidget(self.view)

        side, sl = card(margins=12)
        side.setMinimumWidth(400)
        side.setMaximumWidth(460)
        self.player = Player(compact=True)
        self.player.video.setMinimumHeight(300)
        sl.addWidget(self.player, 1)
        cmp_row = QHBoxLayout()
        self.btn_clip = QPushButton("Clip")
        self.btn_clip.setCheckable(True)
        self.btn_clip.setChecked(True)
        self.btn_orig = QPushButton("Original")
        self.btn_orig.setCheckable(True)
        self.btn_orig.setToolTip("Compare: play the same moment from the source video")
        cmp_row.addWidget(self.btn_clip)
        cmp_row.addWidget(self.btn_orig)
        sl.addLayout(cmp_row)
        self.title = label("Select a clip", "h2", wrap=True)
        sl.addWidget(self.title)
        self.meta = QFormLayout()
        self.meta_l = {}
        for k in ("Source", "Timestamps", "Format", "Size", "Selection", "Quality check", "Repairs", "Tags"):
            v = label("–", wrap=True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.meta_l[k] = v
            self.meta.addRow(label(k, "muted"), v)
        sl.addLayout(self.meta)
        r1 = QHBoxLayout()
        self.btn_ok = QPushButton("Approve (A)")
        self.btn_no = QPushButton("Reject (R)")
        self.btn_edit = QPushButton("Needs edit (E)")
        for b in (self.btn_ok, self.btn_no, self.btn_edit):
            r1.addWidget(b)
        sl.addLayout(r1)
        r2 = QHBoxLayout()
        self.btn_rename = QPushButton("Rename")
        self.btn_tag = QPushButton("Tag")
        self.btn_reedit = QPushButton("Re-edit")
        self.btn_reedit.setToolTip("Open the source video in the Timeline Editor at this clip's range")
        self.btn_regen = QPushButton("Regenerate")
        self.btn_regen.setToolTip("Export this range again with the current version of its preset")
        for b in (self.btn_rename, self.btn_tag, self.btn_reedit, self.btn_regen):
            r2.addWidget(b)
        sl.addLayout(r2)
        r3 = QHBoxLayout()
        self.btn_show = QPushButton("Show in Finder")
        self.btn_play = QPushButton("Open in player")
        self.btn_del = QPushButton("Delete file")
        self.btn_del.setObjectName("danger")
        self.btn_del.setToolTip("Delete this exported clip file. Source videos are never touched.")
        for b in (self.btn_show, self.btn_play, self.btn_del):
            r3.addWidget(b)
        sl.addLayout(r3)
        split.addWidget(side)
        split.setStretchFactor(0, 1)
        root.addWidget(split, 1)
        self.count = label("", "muted")
        root.addWidget(self.count)

        self.tabs.currentChanged.connect(lambda i: self.refresh())
        self.search.textChanged.connect(lambda t: self.refresh())
        self.f_project.currentIndexChanged.connect(lambda i: self.refresh())
        self.sort.currentIndexChanged.connect(lambda i: self.refresh())
        self.view.selectionModel().selectionChanged.connect(self.show_details)
        self.view.customContextMenuRequested.connect(self.menu)
        self.view.doubleClicked.connect(lambda i: self.player.toggle())
        self.btn_ok.clicked.connect(lambda: self.set_review("approved"))
        self.btn_no.clicked.connect(lambda: self.set_review("rejected"))
        self.btn_edit.clicked.connect(lambda: self.set_review("needs_edit"))
        self.btn_rename.clicked.connect(self.rename)
        self.btn_tag.clicked.connect(self.tag)
        self.btn_reedit.clicked.connect(self.re_edit)
        self.btn_regen.clicked.connect(self.regenerate)
        self.btn_show.clicked.connect(lambda: self.one() and reveal(self.one()["out_path"]))
        self.btn_play.clicked.connect(lambda: self.one() and open_path(self.one()["out_path"]))
        self.btn_del.clicked.connect(self.delete_files)
        self.btn_folder.clicked.connect(self.open_output)
        self.btn_deliver.clicked.connect(self.deliver)
        self.btn_report.clicked.connect(self.report)
        self.btn_clip.clicked.connect(lambda: self.compare(False))
        self.btn_orig.clicked.connect(lambda: self.compare(True))
        for k, fn in {"A": lambda: self.set_review("approved"), "R": lambda: self.set_review("rejected"),
                      "E": lambda: self.set_review("needs_edit"), "Space": self.player.toggle}.items():
            s = QShortcut(QKeySequence(k), self)
            s.setContext(Qt.WidgetWithChildrenShortcut)
            s.activated.connect(fn)
        self.reload_projects()
        self.refresh()
        self.show_details()

    # ---- data
    def reload_projects(self):
        cur = self.f_project.currentData()
        self.f_project.blockSignals(True)
        self.f_project.clear()
        self.f_project.addItem("All projects", None)
        for r in db.projects():
            self.f_project.addItem(r["name"], r["id"])
        self.f_project.setCurrentIndex(max(0, self.f_project.findData(cur)))
        self.f_project.blockSignals(False)

    def _where(self):
        cat = CATS[self.tabs.currentIndex()][0]
        where = ["(j.status='done' OR j.review='failed_qc')", "j.out_path IS NOT NULL"]
        args = []
        if cat != "all":
            where.append("j.review=?")
            args.append(cat)
        t = self.search.text().strip()
        if t:
            where.append("(j.out_path LIKE ? OR v.name LIKE ? OR j.tags LIKE ? OR j.title LIKE ?)")
            args += [f"%{t}%"] * 4
        if self.f_project.currentData():
            where.append("b.project_id=?")
            args.append(self.f_project.currentData())
        return " AND ".join(where), args

    def refresh(self):
        sel = {r["id"] for r in self.selected()}
        where, args = self._where()
        order = ["j.finished_at DESC", "j.finished_at", "(j.end-j.start) DESC", "(j.end-j.start)",
                 "v.name, j.start"][self.sort.currentIndex()]
        rows = db.q(f"SELECT j.*, v.name vname, v.path vpath, v.fps vfps FROM jobs j LEFT JOIN videos v ON "
                    f"v.id=j.video_id LEFT JOIN batches b ON b.id=j.batch_id WHERE {where} ORDER BY {order} "
                    f"LIMIT {PAGE}", args)
        total = db.q1(f"SELECT COUNT(*) c FROM jobs j LEFT JOIN videos v ON v.id=j.video_id LEFT JOIN batches b "
                      f"ON b.id=j.batch_id WHERE {where}", args)["c"]
        if [r["id"] for r in rows] != [r["id"] for r in self.model.rows] or \
                [r["review"] for r in rows] != [r["review"] for r in self.model.rows]:
            self.model.beginResetModel()
            self.model.rows = rows
            self.model.endResetModel()
            sm = self.view.selectionModel()
            for i, r in enumerate(rows):
                if r["id"] in sel:
                    sm.select(self.model.index(i), sm.Select)
        counts = {r["review"]: r["c"] for r in db.q(
            "SELECT review, COUNT(*) c FROM jobs WHERE (status='done' OR review='failed_qc') AND out_path IS NOT "
            "NULL GROUP BY review")}
        for i, (k, name) in enumerate(CATS):
            n = sum(counts.values()) if k == "all" else counts.get(k, 0)
            self.tabs.setTabText(i, f"{name}  {n}")
        self.count.setText(f"Showing {len(rows)} of {total:,} clips" + (" (refine the search to see more)"
                                                                        if total > PAGE else ""))

    def selected(self):
        return [i.data(Qt.UserRole) for i in self.view.selectionModel().selectedIndexes()]

    def one(self):
        s = self.selected()
        return s[0] if len(s) == 1 else None

    def show_details(self, *a):
        sel = self.selected()
        r = sel[0] if len(sel) == 1 else None
        for b in (self.btn_rename, self.btn_reedit, self.btn_show, self.btn_play, self.btn_clip, self.btn_orig):
            b.setEnabled(r is not None)
        for b in (self.btn_ok, self.btn_no, self.btn_edit, self.btn_tag, self.btn_regen, self.btn_del):
            b.setEnabled(bool(sel))
        if r is None:
            self.title.setText(f"{len(sel)} clips selected" if sel else "Select a clip")
            for v in self.meta_l.values():
                v.setText("–")
            self.player.unload()
            return
        self.title.setText(r["title"] or Path(r["out_path"]).name)
        pr = json.loads(r["preset"])
        qc = json.loads(r["qc"]) if r["qc"] else None
        if qc:
            info = qc.get("info", {})
            qtxt = ("Passed" if qc["ok"] else "Failed: " + "; ".join(qc["issues"]))
            if qc.get("warnings"):
                qtxt += " · notes: " + "; ".join(qc["warnings"])
            if info.get("size"):
                qtxt += f" · {info['size'][0]}×{info['size'][1]}, {info.get('codec', '')}, " \
                        f"{'audio' if info.get('audio') else 'no audio'}"
        else:
            qtxt = "Not checked (exported before quality checks existed)"
        exists = Path(r["out_path"]).exists()
        vals = {"Source": r["vname"] or "(removed from library)",
                "Timestamps": f"{fmt_dur(r['start'], True)} → {fmt_dur(r['end'], True)} ({r['end'] - r['start']:.1f}s)",
                "Format": f"{pr.get('name', '')} · {pr.get('aspect')} · {pr.get('resolution')} · "
                          f"{pr.get('fps')} fps · {pr.get('codec', '').upper()}",
                "Size": fmt_size(r["out_size"]) if exists else "File not found (moved or deleted)",
                "Selection": (r["selection"] or "") + (f" · score {r['score']:.0f}" if r["score"] is not None else ""),
                "Quality check": qtxt,
                "Repairs": "; ".join(json.loads(r["repairs"])) if r["repairs"] else "None needed",
                "Tags": r["tags"] or "–"}
        for k, v in vals.items():
            self.meta_l[k].setText(v)
        self.btn_clip.setChecked(True)
        self.btn_orig.setChecked(False)
        self.player.load(r["out_path"] if exists else None, 30)

    def compare(self, original):
        r = self.one()
        if not r:
            return
        self.btn_clip.setChecked(not original)
        self.btn_orig.setChecked(original)
        if original:
            if not r["vpath"] or not Path(r["vpath"]).exists():
                QMessageBox.information(self, "Original not available", "The source video is missing.")
                self.compare(False)
                return
            self.player.load(r["vpath"], r["vfps"])
            self.player.play_range(r["start"], r["end"])
        else:
            self.player.load(r["out_path"], 30)
            self.player.player.play()

    # ---- actions
    def set_review(self, state):
        rows = self.selected()
        if not rows:
            return
        learn = db.setting("learning_enabled", "1") == "1"
        for r in rows:
            if r["review"] == "failed_qc" and state != "rejected":
                continue
            db.run("UPDATE jobs SET review=? WHERE id=?", (state, r["id"]))
            if learn and state in ("approved", "rejected"):
                feats = dict(length=r["end"] - r["start"], mode=r["selection"], score=r["score"],
                             preset=json.loads(r["preset"]).get("name"))
                db.run("INSERT INTO feedback(ts,job_id,decision,features) VALUES(?,?,?,?)",
                       (time.time(), r["id"], state, json.dumps(feats)))
        self.refresh()
        self.show_details()

    def rename(self):
        r = self.one()
        if not r:
            return
        old = Path(r["out_path"])
        name, ok = QInputDialog.getText(self, "Rename clip", "New file name:", text=old.stem)
        if not ok or not name.strip() or name.strip() == old.stem:
            return
        from ..export import safe_name
        new = old.with_name(safe_name(name.strip()) + old.suffix)
        if new.exists():
            QMessageBox.warning(self, "Name taken", "A file with that name already exists in this folder.")
            return
        try:
            if old.exists():
                old.rename(new)
            for ext in (".srt", ".vtt"):
                if old.with_suffix(ext).exists():
                    old.with_suffix(ext).rename(new.with_suffix(ext))
        except OSError as e:
            QMessageBox.warning(self, "Couldn't rename", str(e))
            return
        db.run("UPDATE jobs SET out_path=?, title=? WHERE id=?", (str(new), name.strip(), r["id"]))
        self.refresh()
        self.show_details()

    def tag(self):
        rows = self.selected()
        if not rows:
            return
        cur = rows[0]["tags"] if len(rows) == 1 else ""
        t, ok = QInputDialog.getText(self, "Tags", "Tags (comma separated):", text=cur or "")
        if ok:
            tags = ", ".join(x.strip() for x in t.split(",") if x.strip())
            db.many("UPDATE jobs SET tags=? WHERE id=?", [(tags, r["id"]) for r in rows])
            self.refresh()
            self.show_details()

    def re_edit(self):
        r = self.one()
        if r:
            self.reEdit.emit(r["video_id"], r["start"], r["end"])

    def regenerate(self):
        rows = self.selected()
        if not rows:
            return
        presets = db.presets()
        n = 0
        for r in rows:
            pr = json.loads(r["preset"])
            name = pr.pop("name", "")
            if name in presets:              # use the preset as it is now, keep clip-specific framing options
                keep = {k: pr[k] for k in ("reframe_mode", "track_sensitivity", "subtitles", "sidecar_captions",
                                           "caption_style", "caption_position", "reframe_x") if k in pr}
                pr = dict(presets[name], **keep)
            vid = db.q1("SELECT project_id FROM videos WHERE id=?", (r["video_id"],))
            if not vid:
                continue
            queue.add_manual_jobs(r["video_id"], [(r["start"], r["end"])], name, pr,
                                  db.project_name(vid["project_id"]))
            db.run("UPDATE jobs SET review='rejected' WHERE id=? AND review IN ('needs_edit','failed_qc')",
                   (r["id"],))
            n += 1
        QMessageBox.information(self, "Regenerating", f"{n} clip(s) were added to the queue again.")
        self.refresh()

    def delete_files(self):
        rows = self.selected()
        if not rows or not confirm(self, "Delete clip files",
                                   f"Delete {len(rows)} exported clip file(s) from disk?\n\nOnly the exported "
                                   "clips are deleted. Your source videos are never touched."):
            return
        sources = {r["path"] for r in db.q("SELECT path FROM videos")}
        for r in rows:
            p = Path(r["out_path"])
            if str(p) in sources:          # never delete a library source, whatever happens
                continue
            for f in (p, p.with_suffix(".srt"), p.with_suffix(".vtt")):
                f.unlink(missing_ok=True)
            if r["thumb"]:
                Path(r["thumb"]).unlink(missing_ok=True)
            db.run("DELETE FROM jobs WHERE id=?", (r["id"],))
        self.refresh()
        self.show_details()

    def open_output(self):
        d = Path(db.setting("output_dir"))
        d.mkdir(parents=True, exist_ok=True)
        open_path(d)

    def _tab_rows(self):
        rows = self.selected()
        if rows:
            return rows
        where, args = self._where()
        return db.q(f"SELECT j.*, v.name vname FROM jobs j LEFT JOIN videos v ON v.id=j.video_id LEFT JOIN batches b "
                    f"ON b.id=j.batch_id WHERE {where} ORDER BY j.id", args)

    def deliver(self):
        rows = [r for r in self._tab_rows() if Path(r["out_path"]).exists()]
        if not rows:
            QMessageBox.information(self, "Nothing to copy", "There are no clip files in this view.")
            return
        d = QFileDialog.getExistingDirectory(self, f"Copy {len(rows)} clip(s) to…")
        if not d:
            return
        free = shutil.disk_usage(d).free
        need = sum(r["out_size"] or 0 for r in rows)
        if need > free:
            QMessageBox.warning(self, "Not enough space", f"These clips need {fmt_size(need)} but the destination "
                                                          f"has {fmt_size(free)} free.")
            return
        done = 0
        for r in rows:
            src = Path(r["out_path"])
            dst = Path(d) / src.name
            k = 2
            while dst.exists():
                dst = Path(d) / f"{src.stem} ({k}){src.suffix}"
                k += 1
            shutil.copy2(src, dst)
            for ext in (".srt", ".vtt"):
                if src.with_suffix(ext).exists():
                    shutil.copy2(src.with_suffix(ext), dst.with_suffix(ext))
            done += 1
        QMessageBox.information(self, "Copied", f"Copied {done} clip(s) to {d}.")

    def report(self):
        rows = self._tab_rows()
        path, filt = QFileDialog.getSaveFileName(self, "Save report", str(Path.home() / "Reels Cutter report.csv"),
                                                 "CSV (*.csv);;HTML (*.html)")
        if not path:
            return
        cols = ["clip", "source video", "start", "end", "length_s", "preset", "status", "review", "tags",
                "quality", "size_bytes", "path"]
        data = []
        for r in rows:
            qc = json.loads(r["qc"]) if r["qc"] else None
            data.append([Path(r["out_path"]).name, r["vname"], fmt_dur(r["start"], True), fmt_dur(r["end"], True),
                         round(r["end"] - r["start"], 2), json.loads(r["preset"]).get("name"), r["status"],
                         r["review"], r["tags"], ("passed" if qc and qc["ok"] else "; ".join(qc["issues"]) if qc
                                                  else "not checked"), r["out_size"], r["out_path"]])
        if path.lower().endswith(".html"):
            body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c if c is not None else ''))}</td>" for c in row)
                           + "</tr>" for row in data)
            Path(path).write_text(f"<!doctype html><meta charset=utf-8><title>Reels Cutter report</title>"
                                  f"<style>body{{font:13px -apple-system,sans-serif;background:#0f1115;color:#e8eaef}}"
                                  f"td,th{{padding:4px 8px;border-bottom:1px solid #262c37;text-align:left}}</style>"
                                  f"<h2>Reels Cutter report — {len(data)} clips</h2><table><tr>"
                                  + "".join(f"<th>{c}</th>" for c in cols) + f"</tr>{body}</table>", encoding="utf-8")
        else:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(cols)
                w.writerows(data)
        reveal(path)

    def menu(self, pos):
        if not self.selected():
            return
        m = QMenu(self)
        m.addAction("Approve", lambda: self.set_review("approved"))
        m.addAction("Reject", lambda: self.set_review("rejected"))
        m.addAction("Needs editing", lambda: self.set_review("needs_edit"))
        m.addAction("Mark ready to export", lambda: self.set_review("ready"))
        m.addSeparator()
        m.addAction("Tag…", self.tag)
        m.addAction("Regenerate", self.regenerate)
        if self.one():
            m.addAction("Rename…", self.rename)
            m.addAction("Re-edit in timeline", self.re_edit)
            m.addAction("Show in Finder", lambda: reveal(self.one()["out_path"]))
        m.addSeparator()
        m.addAction("Delete file…", self.delete_files)
        m.exec(self.view.viewport().mapToGlobal(pos))

    def leave(self):
        self.player.pause()
