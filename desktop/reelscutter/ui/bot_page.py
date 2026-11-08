"""Jasper Bot page: chat with the assistant that runs the app, plus quick actions that work without a key."""
import html
import json
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLineEdit, QPushButton, QTextBrowser, QVBoxLayout, QWidget)

from .. import db, jasperbot
from . import theme
from .widgets import card, confirm, label

QUICK = [("Status", "get_status", {}, "What's going on right now?"),
         ("Latest clips", "list_clips", {"limit": 10}, "Show my latest clips."),
         ("Check folders", "check_folders", {}, "Check my folders for new videos."),
         ("Retry failed", "queue_control", {"action": "retry_failed"}, "Retry the clips that failed."),
         ("Posting report", "posting_report", {}, "How are my posts doing and when do the next ones go out?")]

NICE = {"get_status": "checking the status", "list_videos": "looking at your videos", "list_clips": "looking at clips",
        "explain_clip": "reading what was changed", "make_clips": "queuing clips", "review_clips": "updating review",
        "plan_posts": "planning posts", "send_posts": "sending posts", "posting_report": "reading post stats",
        "set_posting": "changing posting settings", "add_autopilot_folder": "setting up autopilot",
        "check_folders": "checking folders", "queue_control": "controlling the queue", "open_page": "opening a page"}


def _summary(name, out):
    """Plain-words result for quick actions (no AI needed)."""
    if name == "get_status":
        s = out
        lines = [f"Exporting {s['exporting']}, waiting {s['waiting']}, finished {s['finished']}, "
                 f"failed {s['failed']}" + (" · paused" if s["paused"] else ""),
                 f"{s['videos']} video(s) in the library, {s['clips_awaiting_review']} clip(s) to review"]
        lines.append("Autopilot folders: " + (", ".join(s["autopilot_folders"]) or "none yet"))
        lines.append("Posting: " + ("connected" if s["posting_connected"] else "not connected yet")
                     + f", {s['posts_planned']} planned" + (f", next {s['next_post']}" if s["next_post"] else "")
                     + (f", {s['posts_failed']} failed" if s["posts_failed"] else ""))
        if s["blocked_reason"]:
            lines.append("Waiting: " + s["blocked_reason"])
        if s["recent_errors"]:
            lines.append("Recent problem: " + s["recent_errors"][0])
        return "\n".join(lines)
    if name == "list_clips":
        if not out:
            return "No clips yet."
        return "\n".join(f"#{c['clip_id']} {c['video']} {c['from']:.0f}–{c['to']:.0f}s · {c['status']}"
                         + (f" · {c['review']}" if c["status"] == "done" else "")
                         + (f" · post {c['post']}" if c["post"] else "") for c in out)
    if name == "check_folders":
        return "Checked: " + (", ".join(out["checked"]) or "no folders set up yet") + ". " + out["note"]
    if name == "queue_control":
        return "Failed clips are queued again." if out["done"] == "retry_failed" else f"Queue {out['done']}d."
    if name == "posting_report":
        hrs = ", ".join(f"{int(h)}:00" for h in out["best_hours"])
        posts = out["posts"]
        lines = [f"Best hours: {hrs}"]
        for p in posts[-10:]:
            lines.append(f"{p['when']} · {p['video'] or 'clip'} · {p['status']}"
                         + (f" · {p['views']:,} views" if p["views"] is not None else ""))
        if not posts:
            lines.append("No posts yet.")
        return "\n".join(lines)
    return json.dumps(out, default=str)[:1500]


class BotPage(QWidget):
    navigate = Signal(str)
    _reply = Signal(str, str)
    _ask_user = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("page")
        self._answer = None
        self._ev = threading.Event()
        self.bot = jasperbot.Bot(confirm=self._confirm_from_worker,
                                 navigate=lambda p: self._reply.emit("nav", p))
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(label("Jasper Bot", "h1"))
        top.addStretch()
        self.btn_new = QPushButton("New chat")
        top.addWidget(self.btn_new)
        root.addLayout(top)
        self.hint = label("", "muted", wrap=True)
        root.addWidget(self.hint)

        cc, cl = card(margins=12)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setObjectName("chat")
        cl.addWidget(self.view, 1)
        qh = QHBoxLayout()
        self.quick = []
        for title, tool, args, question in QUICK:
            b = QPushButton(title)
            b.setObjectName("chip")
            b.clicked.connect(lambda _=False, t=tool, a=args, q=question, n=title: self.quick_action(t, a, q, n))
            qh.addWidget(b)
            self.quick.append(b)
        qh.addStretch()
        cl.addLayout(qh)
        ih = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask Jasper Bot… e.g. “make 5 clips of 30 seconds from my newest video”")
        self.input.setMinimumHeight(40)
        self.btn_send = QPushButton("Send")
        self.btn_send.setObjectName("primary")
        self.btn_send.setMinimumHeight(40)
        ih.addWidget(self.input, 1)
        ih.addWidget(self.btn_send)
        cl.addLayout(ih)
        root.addWidget(cc, 1)

        self.btn_send.clicked.connect(self.send)
        self.input.returnPressed.connect(self.send)
        self.btn_new.clicked.connect(self.new_chat)
        self._reply.connect(self._on_reply)
        self._ask_user.connect(self._on_ask)
        self.html = []
        self.refresh()
        self._add("bot", "Hi Jasper! I can check what the app is doing, make clips, explain what was changed in a "
                         "clip, plan and send posts, set up autopilot folders and control the queue. "
                         "Ask me anything, or tap a quick button.")

    # ---- chat rendering
    def _add(self, who, text):
        t = html.escape(text).replace("\n", "<br>")
        if who == "you":
            self.html.append(f"<table width='100%' cellspacing='0' cellpadding='0'><tr><td width='25%'></td><td "
                             f"style='background:{theme.ACCENT};color:white;padding:9px 13px;border-radius:12px'>"
                             f"{t}</td></tr></table>")
        elif who == "step":
            self.html.append(f"<p style='color:{theme.MUTED};margin:2px 0 2px 6px'>· {t}</p>")
        else:
            self.html.append(f"<table width='100%' cellspacing='0' cellpadding='0'><tr><td style='background:"
                             f"{theme.PANEL2};padding:9px 13px;border-radius:12px'><b style='color:{theme.ACCENT2}'>"
                             f"Jasper Bot</b><br>{t}</td><td width='20%'></td></tr></table>")
        self.html.append("<div style='height:8px'></div>")
        self.view.setHtml("".join(self.html))
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def refresh(self):
        self.hint.setText("Chatting uses Claude with your Anthropic API key (Settings → AI). Quick buttons work "
                          "without it. Jasper Bot asks before it posts anything or changes settings."
                          if not self.bot.available() else
                          "Jasper Bot uses the same controls as the app. It asks before it posts anything or "
                          "changes settings.")

    def _busy(self, on):
        for w in [self.btn_send, self.input] + self.quick:
            w.setEnabled(not on)

    # ---- actions
    def send(self):
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self._add("you", text)
        if not self.bot.available():
            self._add("bot", "To chat I need your Anthropic API key in Settings → AI. The quick buttons below work "
                             "right now.")
            return
        self._busy(True)

        def run():
            try:
                out = self.bot.ask(text, on_step=lambda n: self._reply.emit("step", NICE.get(n, n)))
            except Exception as e:
                out = f"Sorry, something went wrong: {e}"
                db.log("error", f"Jasper Bot: {e}")
            self._reply.emit("bot", out)
        threading.Thread(target=run, daemon=True, name="jasperbot").start()

    def quick_action(self, tool, args, question, title):
        self._add("you", question)
        if self.bot.available():
            self.input.setText(question)
            self.send()
            return
        self._busy(True)

        def run():
            try:
                out = _summary(tool, self.bot.run_tool(tool, dict(args)))
            except Exception as e:
                out = f"Couldn't do that: {e}"
            self._reply.emit("bot", out)
        threading.Thread(target=run, daemon=True).start()

    def new_chat(self):
        self.bot.reset()
        self.html = []
        self._add("bot", "New chat. What should we do?")

    def _on_reply(self, kind, text):
        if kind == "nav":
            self.navigate.emit(text)
            return
        if kind == "step":
            self._add("step", text)
            return
        self._busy(False)
        self._add("bot", text)
        self.input.setFocus()

    # ---- asking the user from the worker thread
    def _confirm_from_worker(self, text):
        self._ev.clear()
        self._answer = False
        self._ask_user.emit(text)
        self._ev.wait(600)
        return bool(self._answer)

    def _on_ask(self, text):
        self._answer = confirm(self, "Jasper Bot", text, ok="Yes, do it", danger=False)
        self._add("step", "you said " + ("yes" if self._answer else "no"))
        self._ev.set()
