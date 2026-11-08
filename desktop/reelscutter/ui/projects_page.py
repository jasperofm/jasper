"""Projects: group videos and clips; create, rename, delete, and see each project's health at a glance."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QInputDialog, QMessageBox, QPushButton,
                               QTableView, QVBoxLayout, QWidget)

from .. import db
from .tables import RowsModel
from .widgets import confirm, fmt_dur, fmt_size, label


class ProjectsPage(QWidget):
    openProject = Signal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(label("Projects", "h1"))
        top.addStretch()
        self.btn_new = QPushButton("New project")
        self.btn_open = QPushButton("Show videos")
        self.btn_rename = QPushButton("Rename")
        self.btn_del = QPushButton("Delete")
        self.btn_del.setObjectName("danger")
        for b in (self.btn_open, self.btn_rename, self.btn_del, self.btn_new):
            top.addWidget(b)
        self.btn_new.setObjectName("primary")
        root.addLayout(top)
        cols = [("Project", lambda r: r["name"]), ("Videos", lambda r: f"{r['videos']:,}"),
                ("Footage", lambda r: fmt_dur(r["footage"] or 0)), ("Clips", lambda r: f"{r['clips']:,}"),
                ("Approved", lambda r: f"{r['approved']:,}"), ("Queued", lambda r: f"{r['queued']:,}"),
                ("Failed", lambda r: f"{r['failed']:,}"), ("Missing sources", lambda r: f"{r['missing']:,}"),
                ("Output size", lambda r: fmt_size(r["size"] or 0))]
        self.model = RowsModel(cols)
        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.verticalHeader().hide()
        self.view.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.view.setShowGrid(False)
        root.addWidget(self.view, 1)
        root.addWidget(label("Deleting a project only removes the grouping: its videos move to “Default”, and no "
                             "files are deleted.", "muted", wrap=True))
        self.btn_new.clicked.connect(self.new)
        self.btn_rename.clicked.connect(self.rename)
        self.btn_del.clicked.connect(self.delete)
        self.btn_open.clicked.connect(lambda: self.cur() and self.openProject.emit(self.cur()["id"]))
        self.view.doubleClicked.connect(lambda i: self.openProject.emit(self.model.rows[i.row()]["id"]))
        self.refresh()

    def cur(self):
        rows = self.view.selectionModel().selectedRows()
        return self.model.rows[rows[0].row()] if rows else None

    def refresh(self):
        rows = db.q("""SELECT p.id, p.name,
            (SELECT COUNT(*) FROM videos v WHERE v.project_id=p.id) videos,
            (SELECT SUM(duration) FROM videos v WHERE v.project_id=p.id) footage,
            (SELECT COUNT(*) FROM videos v WHERE v.project_id=p.id AND v.status='missing') missing,
            (SELECT COUNT(*) FROM jobs j JOIN batches b ON b.id=j.batch_id WHERE b.project_id=p.id AND j.status='done') clips,
            (SELECT COUNT(*) FROM jobs j JOIN batches b ON b.id=j.batch_id WHERE b.project_id=p.id AND j.review='approved') approved,
            (SELECT COUNT(*) FROM jobs j JOIN batches b ON b.id=j.batch_id WHERE b.project_id=p.id AND j.status IN ('queued','running')) queued,
            (SELECT COUNT(*) FROM jobs j JOIN batches b ON b.id=j.batch_id WHERE b.project_id=p.id AND j.status='failed') failed,
            (SELECT SUM(out_size) FROM jobs j JOIN batches b ON b.id=j.batch_id WHERE b.project_id=p.id AND j.status='done') size
            FROM projects p ORDER BY p.name""")
        self.model.set_rows(rows)

    def new(self):
        name, ok = QInputDialog.getText(self, "New project", "Project name:")
        if ok and name.strip():
            db.project_id(name.strip())
            self.refresh()

    def rename(self):
        r = self.cur()
        if not r:
            return
        name, ok = QInputDialog.getText(self, "Rename project", "New name:", text=r["name"])
        if ok and name.strip() and name.strip() != r["name"]:
            if db.q1("SELECT 1 FROM projects WHERE name=?", (name.strip(),)):
                QMessageBox.warning(self, "Name taken", "A project with that name already exists.")
                return
            db.run("UPDATE projects SET name=? WHERE id=?", (name.strip(), r["id"]))
            self.refresh()

    def delete(self):
        r = self.cur()
        if not r or r["name"] == "Default":
            return
        if confirm(self, "Delete project", f"Delete the project “{r['name']}”? Its {r['videos']} video(s) move to "
                   "“Default”. No files are deleted."):
            d = db.project_id("Default")
            db.run("UPDATE videos SET project_id=? WHERE project_id=?", (d, r["id"]))
            db.run("UPDATE batches SET project_id=? WHERE project_id=?", (d, r["id"]))
            db.run("DELETE FROM projects WHERE id=?", (r["id"],))
            self.refresh()
