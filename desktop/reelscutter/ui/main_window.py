"""Main window: sidebar navigation, pages, menus, shortcuts, drag-and-drop import and live updates."""
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QStackedWidget, QStatusBar, QTabWidget, QVBoxLayout,
                               QWidget, QLabel)

from .. import analysis, db, jobs, paths
from ..jobs import queue
from . import theme
from .analysis_page import AnalysisPage
from .dashboard import Dashboard
from .dialogs import About, Onboarding
from .editor import EditorPage
from .generator import GeneratorPage
from .library import LibraryPage, open_path
from .presets_page import PresetsPage
from .projects_page import ProjectsPage
from .queue_page import QueuePage
from .review import ReviewPage
from .settings_page import SettingsPage
from .storage_page import StoragePage
from .system_page import SystemPage
from .widgets import label

PAGES = [("dashboard", "Dashboard", "◧"), ("library", "Video Library", "▦"), ("analysis", "AI Analysis", "◎"),
         ("generator", "Clip Generator", "✦"), ("editor", "Timeline Editor", "▭"), ("review", "Clip Review Studio", "▣"),
         ("export", "Export Manager", "⇪"), ("projects", "Projects", "❏"), ("storage", "Storage Manager", "⛁"),
         ("system", "System Monitor", "◔"), ("settings", "Settings", "⚙︎")]


class Bridge(QObject):
    """Engine events arrive on worker threads; re-emit them on the GUI thread."""
    event = Signal(str, dict)


class ExportManager(QWidget):
    def __init__(self, queue_page, presets_page):
        super().__init__()
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.addTab(queue_page, "Queue")
        w = QWidget()
        w.setObjectName("page")
        wl = QVBoxLayout(w)
        wl.setContentsMargins(28, 16, 28, 24)
        wl.addWidget(label("Export presets", "h1"))
        wl.addWidget(presets_page, 1)
        self.tabs.addTab(w, "Presets")
        lay.addWidget(self.tabs)
        self.queue_page, self.presets_page = queue_page, presets_page

    def refresh(self):
        if self.tabs.currentIndex() == 0:
            self.queue_page.refresh()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(paths.APP_NAME)
        self.resize(1440, 900)
        self.setMinimumSize(1100, 700)
        self.setAcceptDrops(True)
        self.importer = None
        self._dirty_library = False

        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(232)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 20, 0, 16)
        brand = QLabel(f"<span style='color:{theme.ACCENT};font-size:20px;font-weight:800'>●</span>"
                       f"&nbsp;<span style='font-size:17px;font-weight:700'>Reels Cutter</span>")
        brand.setContentsMargins(20, 0, 0, 14)
        sl.addWidget(brand)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        for i, (key, name, icon) in enumerate(PAGES):
            it = QListWidgetItem(f"{icon}   {name}")
            sc = f"⌘{(i + 1) % 10}" if i < 10 else "⌘,"
            it.setToolTip(f"{name} ({sc})")
            self.nav.addItem(it)
        sl.addWidget(self.nav, 1)
        self.side_status = label("", "muted", wrap=True)
        self.side_status.setContentsMargins(20, 0, 16, 0)
        sl.addWidget(self.side_status)
        h.addWidget(side)

        self.stack = QStackedWidget()
        h.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.p = {}
        self.p["dashboard"] = Dashboard()
        self.p["library"] = LibraryPage()
        self.p["analysis"] = AnalysisPage()
        self.p["generator"] = GeneratorPage()
        self.p["editor"] = EditorPage()
        self.p["review"] = ReviewPage()
        self.queue_page = QueuePage()
        self.presets_page = PresetsPage()
        self.p["export"] = ExportManager(self.queue_page, self.presets_page)
        self.p["projects"] = ProjectsPage()
        self.p["storage"] = StoragePage()
        self.p["system"] = SystemPage()
        self.p["settings"] = SettingsPage()
        for key, _, _ in PAGES:
            self.stack.addWidget(self.p[key])

        sb = QStatusBar()
        self.setStatusBar(sb)
        self.imp_label = QLabel("")
        self.imp_bar = QProgressBar()
        self.imp_bar.setFixedWidth(200)
        self.imp_cancel = QPushButton("Stop import")
        for w in (self.imp_label, self.imp_bar, self.imp_cancel):
            sb.addPermanentWidget(w)
            w.hide()
        self.imp_cancel.clicked.connect(self.stop_import)

        # wiring between pages
        self.nav.currentRowChanged.connect(self._page_changed)
        d = self.p["dashboard"]
        d.navigate.connect(self.go)
        d.importRequested.connect(self.p["library"].import_files)
        lib = self.p["library"]
        lib.importRequested.connect(self.start_import)
        lib.openInEditor.connect(self.open_editor)
        lib.createClips.connect(self.open_generator)
        self.p["generator"].started.connect(self._generation_started)
        self.p["review"].reEdit.connect(self.re_edit)
        self.p["analysis"].openAt.connect(self.open_at)
        self.p["projects"].openProject.connect(self.show_project)
        self.p["settings"].showOnboarding.connect(self.onboarding)
        self.p["settings"].showAbout.connect(lambda: About(self).exec())
        self.presets_page.list.currentTextChanged.connect(lambda *_: None)
        self.p["export"].tabs.currentChanged.connect(lambda i: (self.p["generator"].reload(),
                                                                self.p["editor"].reload_presets()))

        self.bridge = Bridge()
        self.bridge.event.connect(self.on_event, Qt.QueuedConnection)
        jobs.events.subscribe(lambda k, data: self.bridge.event.emit(k, data))

        self._menus()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.nav.setCurrentRow(0)

    # ---- menus & shortcuts
    def _menus(self):
        mb = self.menuBar()
        f = mb.addMenu("File")
        self._act(f, "Import Videos…", "Ctrl+I", self.p["library"].import_files)
        self._act(f, "Import Folder…", "Ctrl+Shift+I", self.p["library"].import_folder)
        f.addSeparator()
        self._act(f, "Open Output Folder", "Ctrl+Shift+O", self.open_output)
        f.addSeparator()
        self._act(f, "Back Up Library Now", None, self.backup_now)
        q = self._act(f, "Quit Reels Cutter", "Ctrl+Q", self.close)
        q.setMenuRole(QAction.QuitRole)
        e = mb.addMenu("Edit")
        self._act(e, "Find in Library", "Ctrl+F", self.find)
        s = self._act(e, "Settings…", "Ctrl+,", lambda: self.go("settings"))
        s.setMenuRole(QAction.PreferencesRole)
        pr = mb.addMenu("Process")
        self._act(pr, "Clip Generator", "Ctrl+G", lambda: self.go("generator"))
        self._act(pr, "Pause / Resume All", "Ctrl+P", self.toggle_pause)
        self._act(pr, "Retry All Failed", None, lambda: (queue.retry_all_failed(), self.go("export")))
        v = mb.addMenu("View")
        for i, (key, name, _) in enumerate(PAGES[:10]):
            self._act(v, name, f"Ctrl+{(i + 1) % 10}", lambda _=False, k=key: self.go(k))
        hm = mb.addMenu("Help")
        a = self._act(hm, "About Reels Cutter", None, lambda: About(self).exec())
        a.setMenuRole(QAction.AboutRole)
        self._act(hm, "Welcome Guide", None, self.onboarding)
        self._act(hm, "Keyboard Shortcuts", "Ctrl+/", lambda: About(self).exec())

    def _act(self, menu, text, key, fn):
        a = QAction(text, self)
        if key:
            a.setShortcut(QKeySequence(key))
        a.triggered.connect(fn)
        menu.addAction(a)
        return a

    # ---- navigation
    def go(self, key):
        idx = [k for k, _, _ in PAGES].index(key)
        self.nav.setCurrentRow(idx)

    def _page_changed(self, i):
        old = self.stack.currentWidget()
        if hasattr(old, "leave"):
            old.leave()
        self.stack.setCurrentIndex(i)
        w = self.stack.currentWidget()
        key = PAGES[i][0]
        if key == "library" and self._dirty_library:
            self.p["library"].reload_filters()
            self.p["library"].refresh()
            self._dirty_library = False
        if key == "generator":
            self.p["generator"].reload()
        if key == "review":
            self.p["review"].reload_projects()
            self.p["review"].refresh()
        if key in ("projects", "analysis", "export", "dashboard") and hasattr(w, "refresh"):
            w.refresh()
        if key == "editor":
            self.p["editor"].reload_presets()
        if key == "settings":
            self.p["settings"].load()

    def open_editor(self, vid):
        self.p["editor"].open_video(vid)
        self.go("editor")

    def re_edit(self, vid, a, b):
        self.open_editor(vid)
        ed = self.p["editor"]
        ed.timeline.set_in_out(a, b)
        ed._update_in_out_labels()
        ed.player.seek(a)

    def open_at(self, vid, t):
        self.open_editor(vid)
        self.p["editor"].player.seek(max(0.0, t - 1))

    def open_generator(self, ids):
        self.p["generator"].set_targets(ids)
        self.go("generator")

    def _generation_started(self):
        self.statusBar().showMessage("Clip generation started. Progress is on the Export Manager → Queue tab.", 8000)
        self.go("export")

    def show_project(self, pid):
        lib = self.p["library"]
        lib.reload_filters()
        i = lib.f_project.findData(pid)
        if i >= 0:
            lib.f_project.setCurrentIndex(i)
        self.go("library")

    def find(self):
        self.go("library")
        self.p["library"].search.setFocus()
        self.p["library"].search.selectAll()

    def toggle_pause(self):
        queue.resume() if queue.paused else queue.pause()
        self.statusBar().showMessage("Processing paused." if queue.paused else "Processing resumed.", 4000)

    def open_output(self):
        d = Path(db.setting("output_dir"))
        d.mkdir(parents=True, exist_ok=True)
        open_path(d)

    def backup_now(self):
        from .. import backup
        p = backup.backup("manual")
        self.statusBar().showMessage(f"Library backed up to {p}", 8000)

    def onboarding(self):
        Onboarding(self).exec()
        self.p["dashboard"].refresh()

    # ---- import
    def start_import(self, sources):
        if self.importer and self.importer.is_alive():
            QMessageBox.information(self, "Import running", "Please wait for the current import to finish.")
            return
        proj = self.p["library"].f_project.currentText()
        proj = proj if proj and proj != "All projects" else "Default"
        self.importer = jobs.Importer(sources, proj)
        self.importer.start()
        for w in (self.imp_label, self.imp_bar, self.imp_cancel):
            w.show()
        self.imp_label.setText("Scanning…")
        self.imp_bar.setRange(0, 0)

    def stop_import(self):
        if self.importer:
            self.importer.stop = True

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths_ = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths_:
            self.go("library")
            self.start_import(paths_)

    # ---- live updates
    def on_event(self, kind, data):
        if kind == "import_started":
            self.imp_bar.setRange(0, max(1, data["total"]))
            self.imp_label.setText(f"Importing 0 of {data['total']:,}")
        elif kind == "import_progress":
            self.imp_bar.setValue(data["done"])
            self.imp_label.setText(f"Importing {data['done']:,} of {data['total']:,}  ·  {data['added']} added")
            if data["done"] % 25 == 0 or data["done"] == data["total"]:
                self._refresh_library()
        elif kind == "import_finished":
            for w in (self.imp_label, self.imp_bar, self.imp_cancel):
                w.hide()
            self._refresh_library()
            self.p["library"].reload_filters()
            msg = f"Import finished: {data['added']} added"
            if data["duplicates"]:
                msg += f", {data['duplicates']} duplicate(s) skipped"
            if data["skipped"]:
                msg += f", {data['skipped']} already in library"
            if data["failed"]:
                msg += f", {data['failed']} couldn't be read (see System Monitor → Log)"
            self.statusBar().showMessage(msg, 15000)
        elif kind in ("queue_changed", "job_done", "job_failed", "analysis_changed"):
            self._dirty_library = True
        elif kind == "disk_low":
            self.statusBar().showMessage(queue.blocked_reason or "Low disk space: processing paused.", 15000)

    def _refresh_library(self):
        if self.stack.currentWidget() is self.p["library"]:
            self.p["library"].refresh()
        else:
            self._dirty_library = True

    def tick(self):
        w = self.stack.currentWidget()
        key = PAGES[self.stack.currentIndex()][0]
        try:
            if key in ("dashboard", "export", "system", "analysis") and hasattr(w, "refresh"):
                w.refresh()
            elif key == "review" and int(time.time()) % 3 == 0:
                w.refresh()
            elif key == "library" and self._dirty_library and int(time.time()) % 2 == 0:
                self._dirty_library = False
                w.refresh()
        except Exception as e:
            db.log("error", f"UI refresh failed on {key}: {e}")
        run = db.q1("SELECT COUNT(*) c FROM jobs WHERE status='running'")["c"]
        qd = db.q1("SELECT COUNT(*) c FROM jobs WHERE status='queued'")["c"]
        txt = "Paused" if queue.paused else (f"Exporting {run} · {qd} waiting" if run or qd else "Idle")
        if analysis.runner.current:
            txt += " · analyzing"
        if queue.blocked_reason:
            txt += f"\n{queue.blocked_reason}"
        self.side_status.setText(txt)

    def closeEvent(self, e):
        running = db.q1("SELECT COUNT(*) c FROM jobs WHERE status='running'")["c"]
        if running:
            box = QMessageBox(self)
            box.setWindowTitle("Quit Reels Cutter?")
            box.setText(f"{running} clip(s) are exporting. They will start again from the beginning the next "
                        "time you open Reels Cutter. Nothing else is lost.")
            q = box.addButton("Quit", QMessageBox.DestructiveRole)
            box.addButton("Keep working", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is not q:
                e.ignore()
                return
        analysis.runner.cancel_all()
        queue.shutdown()
        e.accept()
