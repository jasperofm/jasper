"""Auto-Posting: connect Ayrshare, plan approved clips into a posting calendar, send them, and see what works."""
import json
import threading
import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView,
                               QLineEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTextEdit,
                               QVBoxLayout, QWidget, QDialog, QDialogButtonBox)

from .. import db, posting
from .widgets import card, confirm, label


class EditPostDialog(QDialog):
    def __init__(self, parent, r):
        super().__init__(parent)
        self.setWindowTitle("Edit post")
        self.resize(520, 360)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        self.title = QLineEdit(r["title"] or "")
        self.title.setMaxLength(100)
        f.addRow("Title (YouTube)", self.title)
        self.text = QTextEdit(r["text"] or "")
        f.addRow("Caption and hashtags", self.text)
        lay.addLayout(f)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class PostingPage(QWidget):
    navigate = Signal(str)
    _done = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        posting.ensure_schema()
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        root.addWidget(label("Auto-Posting", "h1"))
        root.addWidget(label("Approved clips go into a posting calendar at your best times, then Ayrshare posts them "
                             "to the platforms you pick, even when this Mac is off. Stats come back so the "
                             "calendar learns which hours work for you.", "muted", wrap=True))

        # --- connection + rules
        sc, sl = card()
        sl.addWidget(label("Settings", "h2"))
        f = QFormLayout()
        self.key = QLineEdit(db.setting("ayrshare_api_key", "") or "")
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("Paste your Ayrshare API key (Ayrshare dashboard → API Key)")
        kh = QHBoxLayout()
        kh.addWidget(self.key, 1)
        self.btn_test = QPushButton("Save and test")
        kh.addWidget(self.btn_test)
        f.addRow("Ayrshare API key", kh)
        ch = QHBoxLayout()
        self.btn_connect = QPushButton("Connect accounts…")
        self.btn_connect.setToolTip("Opens your Ayrshare dashboard in the browser. Open Social Accounts and log in to "
                                    "Instagram, TikTok, YouTube, X, Threads, Facebook, LinkedIn or Reddit on their "
                                    "own login pages. Reels Cutter never sees your passwords.")
        ch.addWidget(self.btn_connect)
        ch.addWidget(label("Log in on each platform's own page, then press Save and test.", "muted"), 1)
        f.addRow("Accounts", ch)
        self.conn = label("", "muted", wrap=True)
        f.addRow("", self.conn)
        ph = QHBoxLayout()
        chosen = json.loads(db.setting("post_platforms", '["instagram","tiktok","youtube"]'))
        self.plat = {}
        for k, name in posting.PLATFORMS.items():
            cb = QCheckBox(name)
            cb.setChecked(k in chosen)
            self.plat[k] = cb
            ph.addWidget(cb)
        ph.addStretch()
        f.addRow("Post to", ph)
        self.per_day = QSpinBox()
        self.per_day.setRange(1, 24)
        self.per_day.setValue(int(db.setting("posts_per_day", "6")))
        self.per_day.setSuffix(" posts a day")
        self.per_day.setMaximumWidth(220)
        f.addRow("How often", self.per_day)
        wh = QHBoxLayout()
        self.w0, self.w1 = QSpinBox(), QSpinBox()
        for s, k, d in ((self.w0, "post_window_start", "8"), (self.w1, "post_window_end", "22")):
            s.setRange(0, 23)
            s.setValue(int(db.setting(k, d)))
            s.setSuffix(":00")
        wh.addWidget(label("from"))
        wh.addWidget(self.w0)
        wh.addWidget(label("to"))
        wh.addWidget(self.w1)
        wh.addStretch()
        f.addRow("Posting hours", wh)
        self.yt = QComboBox()
        self.yt.addItems(["public", "unlisted", "private"])
        self.yt.setCurrentText(db.setting("youtube_visibility", "public"))
        self.yt.setMaximumWidth(220)
        f.addRow("YouTube visibility", self.yt)
        self.sub = QLineEdit(db.setting("reddit_subreddit", "") or "")
        self.sub.setPlaceholderText("e.g. personalfinance (without r/)")
        self.sub.setMaximumWidth(320)
        rh = QHBoxLayout()
        rh.addWidget(self.sub)
        rh.addWidget(label("Reddit gets a link post (it doesn't take video uploads from posting tools).", "muted"), 1)
        f.addRow("Reddit subreddit", rh)
        self.sub.setToolTip("Reddit doesn't take video uploads from posting tools, so Reddit gets a link post with "
                            "the clip's title. Check the subreddit's rules on self-promotion first.")
        sl.addLayout(f)
        self.best = label("", "muted", wrap=True)
        sl.addWidget(self.best)
        root.addWidget(sc)

        # --- calendar
        cc, cl = card()
        h = QHBoxLayout()
        h.addWidget(label("Posting calendar", "h2"))
        h.addStretch()
        self.btn_add = QPushButton("Add approved clips")
        self.btn_add.setObjectName("primary")
        self.btn_edit = QPushButton("Edit text")
        self.btn_rm = QPushButton("Remove")
        self.btn_rm.setObjectName("danger")
        self.btn_send = QPushButton("Send to Ayrshare")
        self.btn_stats = QPushButton("Refresh stats")
        for b in (self.btn_stats, self.btn_edit, self.btn_rm, self.btn_add, self.btn_send):
            h.addWidget(b)
        cl.addLayout(h)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["When", "Clip", "Platforms", "Caption", "Status", "Views", "Likes"])
        hh = self.table.horizontalHeader()
        for c in (0, 1, 2, 4, 5, 6):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setMinimumHeight(160)
        cl.addWidget(self.table)
        self.msg = label("", "muted", wrap=True)
        cl.addWidget(self.msg)
        root.addWidget(cc, 1)

        self.btn_test.clicked.connect(self.test)
        self.btn_add.clicked.connect(self.add)
        self.btn_rm.clicked.connect(self.remove)
        self.btn_edit.clicked.connect(self.edit)
        self.btn_send.clicked.connect(self.send)
        self.btn_stats.clicked.connect(self.stats)
        for w in [self.per_day, self.w0, self.w1]:
            w.valueChanged.connect(self.save)
        self.yt.currentTextChanged.connect(self.save)
        self.sub.editingFinished.connect(self.save)
        self.btn_connect.clicked.connect(lambda: __import__("webbrowser").open(posting.CONNECT_URL))
        for cb in self.plat.values():
            cb.toggled.connect(self.save)
        self._done.connect(self._finished)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._auto)
        self.timer.start(30 * 60 * 1000)        # pull stats every 30 minutes while the app is open
        self.refresh()

    # --- settings
    def save(self, *_):
        db.set_setting("posts_per_day", self.per_day.value())
        db.set_setting("post_window_start", self.w0.value())
        db.set_setting("post_window_end", self.w1.value())
        db.set_setting("youtube_visibility", self.yt.currentText())
        db.set_setting("reddit_subreddit", self.sub.text().strip())
        db.set_setting("post_platforms", json.dumps([k for k, cb in self.plat.items() if cb.isChecked()]))
        self._best_text()

    def _best_text(self):
        hrs = posting.best_hours(self.per_day.value())
        learned = posting.hour_scores()
        txt = ", ".join(f"{int(h)}:{'30' if h % 1 else '00'}" for h in hrs)
        self.best.setText(f"Posting times each day: {txt}. "
                          + (f"Learned from {len(learned)} hour(s) of your own results." if learned
                             else "These are starting guesses; they change as your stats come in."))

    def test(self):
        db.set_setting("ayrshare_api_key", self.key.text().strip())
        self.conn.setText("Checking…")
        self._bg(lambda: posting.check_connection()[1])

    # --- calendar actions
    def _sel(self):
        return [self.table.item(r.row(), 0).data(Qt.UserRole) for r in self.table.selectionModel().selectedRows()]

    def add(self):
        plats = [k for k, cb in self.plat.items() if cb.isChecked()]
        if not plats:
            self.msg.setText("Tick at least one platform first.")
            return
        ids = [r["id"] for r in db.q(
            "SELECT j.id FROM jobs j LEFT JOIN posts p ON p.job_id=j.id WHERE j.status='done' AND j.hidden=0 "
            "AND j.review='approved' AND p.id IS NULL ORDER BY j.score DESC, j.id")]
        if not ids:
            self.msg.setText("No approved clips waiting. Approve clips in Clip Review Studio first.")
            return
        self.msg.setText(f"Writing post text for {len(ids)} clip(s)…")
        self._bg(lambda: f"Added {posting.add_to_calendar(ids, plats, self.per_day.value())} clip(s) to the calendar. "
                         "Check the text, then press “Send to Ayrshare”.")

    def edit(self):
        sel = self._sel()
        if len(sel) != 1:
            self.msg.setText("Select one post to edit.")
            return
        r = db.q1("SELECT * FROM posts WHERE id=?", (sel[0],))
        if r["status"] == "scheduled":
            self.msg.setText("This post is already with Ayrshare. Remove it and add the clip again to change it.")
            return
        d = EditPostDialog(self, r)
        if d.exec():
            db.run("UPDATE posts SET title=?, text=? WHERE id=?", (d.title.text().strip(), d.text.toPlainText().strip(),
                                                                  r["id"]))
            self.refresh()

    def remove(self):
        sel = self._sel()
        if sel and confirm(self, "Remove posts", f"Remove {len(sel)} post(s) from the calendar? Posts already sent "
                                                 "to Ayrshare are cancelled there too."):
            self._bg(lambda: [posting.cancel(i) for i in sel] and f"Removed {len(sel)} post(s).")

    def send(self):
        if not db.setting("ayrshare_api_key"):
            self.msg.setText("Add your Ayrshare API key above first.")
            return
        n = db.q1("SELECT COUNT(*) c FROM posts WHERE status IN ('planned','failed')")["c"]
        if not n:
            self.msg.setText("Nothing waiting to send.")
            return
        if confirm(self, "Send to Ayrshare", f"Upload {n} clip(s) and schedule them on Ayrshare at the times shown? "
                                             "They will be published to your linked accounts.", ok="Send",
                   danger=False):
            self.msg.setText("Uploading and scheduling…")
            self._bg(lambda: f"Sent {posting.send_all()} post(s). Failed ones show the reason in Status.")

    def stats(self):
        self.msg.setText("Fetching views and likes…")
        self._bg(lambda: f"Updated stats for {posting.refresh_stats(max_age_h=0)} post(s).")

    def _auto(self):
        if db.setting("ayrshare_api_key") and not getattr(self, "_busy", False):
            self._bg(lambda: "" if posting.refresh_stats() == 0 else "Stats updated.")

    def _bg(self, fn):
        self._busy = True
        for b in (self.btn_add, self.btn_send, self.btn_rm, self.btn_stats, self.btn_test):
            b.setEnabled(False)

        def run():
            try:
                out = fn()
            except Exception as e:
                out = str(e)
            self._done.emit(out or "")
        threading.Thread(target=run, daemon=True).start()

    def _finished(self, text):
        self._busy = False
        for b in (self.btn_add, self.btn_send, self.btn_rm, self.btn_stats, self.btn_test):
            b.setEnabled(True)
        if text.startswith(("Connected", "Ayrshare", "Couldn't reach", "Add your")):
            self.conn.setText(text)
        elif text:
            self.msg.setText(text)
        self.refresh()

    def refresh(self):
        rows = db.q("SELECT p.*, v.name vname, j.start js FROM posts p LEFT JOIN jobs j ON j.id=p.job_id "
                    "LEFT JOIN videos v ON v.id=j.video_id WHERE p.at > ? ORDER BY p.at", (time.time() - 14 * 86400,))
        self.table.setRowCount(len(rows))
        names = {"planned": "Planned", "sending": "Sending…", "scheduled": "With Ayrshare", "failed": "Failed"}
        for i, r in enumerate(rows):
            when = datetime.fromtimestamp(r["at"])
            it = QTableWidgetItem(when.strftime("%a %d %b  %H:%M"))
            it.setData(Qt.UserRole, r["id"])
            self.table.setItem(i, 0, it)
            st = names.get(r["status"], r["status"])
            if r["status"] == "scheduled" and r["at"] < time.time():
                st = "Posted"
            if r["status"] == "failed" and r["error"]:
                st = f"Failed: {r['error'][:60]}"
            vals = [f"{r['vname'] or 'clip'} @{int(r['js'] or 0) // 60}:{int(r['js'] or 0) % 60:02d}",
                    ", ".join(posting.PLATFORMS.get(p, p) for p in json.loads(r["platforms"])),
                    (r["text"] or "").replace("\n", " "), st,
                    "" if r["views"] is None else f"{r['views']:,}", "" if r["likes"] is None else f"{r['likes']:,}"]
            for c, t in enumerate(vals, 1):
                cell = QTableWidgetItem(t)
                cell.setToolTip(t)
                self.table.setItem(i, c, cell)
        self._best_text()
        if not db.setting("ayrshare_api_key"):
            self.conn.setText("Not connected yet. Sign up at ayrshare.com, link your social accounts there, "
                              "then paste the API key here.")
