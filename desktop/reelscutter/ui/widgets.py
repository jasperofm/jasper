"""Reusable widgets: stat cards, the editing timeline, progress cells and small helpers."""
from PySide6.QtCore import QRectF, Qt, Signal, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QPainterPath, QFont
from PySide6.QtWidgets import (QFrame, QLabel, QVBoxLayout, QWidget, QStyledItemDelegate, QStyle, QMessageBox,
                               QHBoxLayout, QSizePolicy)

from . import theme


def fmt_dur(t, ms=False):
    if t is None:
        return "–"
    t = max(0.0, float(t))
    h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    sec = f"{s:05.2f}" if ms else f"{int(s):02d}"
    return f"{h}:{m:02d}:{sec}" if h else f"{m:02d}:{sec}"


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def label(text, obj=None, wrap=False):
    lb = QLabel(text)
    if obj:
        lb.setObjectName(obj)
    lb.setWordWrap(wrap)
    return lb


def card(layout_cls=QVBoxLayout, margins=16, spacing=10):
    f = QFrame()
    f.setObjectName("card")
    lay = layout_cls(f)
    lay.setContentsMargins(margins, margins, margins, margins)
    lay.setSpacing(spacing)
    return f, lay


def confirm(parent, title, text, ok="Delete", danger=True):
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    yes = box.addButton(ok, QMessageBox.DestructiveRole if danger else QMessageBox.AcceptRole)
    box.addButton("Cancel", QMessageBox.RejectRole)
    box.exec()
    return box.clickedButton() is yes


class StatCard(QFrame):
    def __init__(self, title, color=theme.ACCENT, tip=""):
        super().__init__()
        self.setObjectName("card")
        self.setMinimumHeight(96)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        self.value = label("0", "statValue")
        self.title = label(title, "statLabel")
        self.sub = label("", "muted")
        dot = QFrame()
        dot.setFixedSize(26, 4)
        dot.setStyleSheet(f"background:{color};border-radius:2px;")
        lay.addWidget(dot)
        lay.addWidget(self.value)
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        if tip:
            self.setToolTip(tip)

    def set(self, value, sub=""):
        self.value.setText(str(value))
        self.sub.setText(sub)
        self.sub.setVisible(bool(sub))


class ProgressDelegate(QStyledItemDelegate):
    """Draws a progress bar in a table cell; the cell's UserRole holds 0..1."""

    def paint(self, p, opt, index):
        if opt.state & QStyle.State_Selected:
            p.fillRect(opt.rect, QColor("#2b3240"))
        v = index.data(Qt.UserRole)
        txt = index.data(Qt.DisplayRole) or ""
        r = opt.rect.adjusted(8, opt.rect.height() // 2 - 4, -52, -(opt.rect.height() // 2 - 4))
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.PANEL2))
        p.drawRoundedRect(r, 4, 4)
        if v:
            rr = QRectF(r)
            rr.setWidth(max(6.0, r.width() * float(v)))
            p.setBrush(QColor(theme.ACCENT))
            p.drawRoundedRect(rr, 4, 4)
        p.setPen(QColor(theme.MUTED))
        p.drawText(opt.rect.adjusted(0, 0, -6, 0), Qt.AlignRight | Qt.AlignVCenter, txt)
        p.restore()


class Timeline(QWidget):
    """Zoomable timeline: ruler, loudness, scene markers, saved segments and draggable in/out handles.

    Mouse: click/drag to scrub, drag the handles to trim, wheel to zoom, shift+wheel to pan,
    click a segment to select it.
    """
    seek = Signal(float)
    inOutChanged = Signal(float, float)
    segmentClicked = Signal(int)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(132)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.duration = 0.0
        self.position = 0.0
        self.in_pt = None
        self.out_pt = None
        self.segments = []      # (id, start, end, label)
        self.selected = None
        self.scenes = []
        self.rms = []
        self.v0, self.v1 = 0.0, 1.0
        self._drag = None

    # ---- data
    def set_video(self, duration, scenes=None, rms=None):
        self.duration = max(0.01, duration or 0.01)
        self.v0, self.v1 = 0.0, self.duration
        self.scenes, self.rms = scenes or [], rms or []
        self.in_pt = self.out_pt = None
        self.selected = None
        self.update()

    def set_analysis(self, scenes, rms):
        self.scenes, self.rms = scenes or [], rms or []
        self.update()

    def set_position(self, t):
        self.position = t
        if t > self.v1 or t < self.v0:      # keep the playhead in view
            span = self.v1 - self.v0
            self.v0 = max(0.0, min(self.duration - span, t - span * 0.1))
            self.v1 = self.v0 + span
        self.update()

    def set_in_out(self, a, b):
        self.in_pt, self.out_pt = a, b
        self.update()

    # ---- geometry
    def _x(self, t):
        w = self.width() - 20
        return 10 + (t - self.v0) / max(1e-6, self.v1 - self.v0) * w

    def _t(self, x):
        w = self.width() - 20
        return max(0.0, min(self.duration, self.v0 + (x - 10) / max(1, w) * (self.v1 - self.v0)))

    # ---- painting
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), QColor(theme.PANEL))
        ruler_h, track_top, track_h = 22, 30, H - 42
        span = self.v1 - self.v0
        # ruler
        steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]
        step = next((s for s in steps if span / s <= max(4, W / 90)), 3600)
        p.setPen(QColor(theme.MUTED))
        p.setFont(QFont(p.font().family(), 9))
        t = (int(self.v0 / step)) * step
        while t <= self.v1:
            x = self._x(t)
            if x >= 10:
                p.drawLine(QPointF(x, ruler_h - 6), QPointF(x, ruler_h))
                p.drawText(QPointF(x + 3, ruler_h - 8), fmt_dur(t))
            t += step
        track = QRectF(10, track_top, W - 20, track_h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.PANEL2))
        p.drawRoundedRect(track, 6, 6)
        # loudness envelope
        if self.rms:
            path = QPainterPath()
            mid = track.center().y()
            lo = min(self.rms)
            rng = max(1.0, max(self.rms) - lo)
            first = True
            n = len(self.rms)
            i0, i1 = max(0, int(self.v0)), min(n, int(self.v1) + 1)
            stride = max(1, (i1 - i0) // max(1, int(W)))
            pts = []
            for i in range(i0, i1, stride):
                x = self._x(i)
                a = (self.rms[i] - lo) / rng * (track_h / 2 - 4)
                pts.append((x, a))
            for x, a in pts:
                if first:
                    path.moveTo(x, mid - a)
                    first = False
                else:
                    path.lineTo(x, mid - a)
            for x, a in reversed(pts):
                path.lineTo(x, mid + a)
            path.closeSubpath()
            p.setBrush(QColor(92, 168, 255, 60))
            p.drawPath(path)
        # scene markers
        p.setPen(QPen(QColor(245, 184, 75, 140), 1))
        for s in self.scenes:
            if self.v0 <= s <= self.v1:
                x = self._x(s)
                p.drawLine(QPointF(x, track.top() + 2), QPointF(x, track.top() + 10))
        # saved segments
        for sid, a, b, lb in self.segments:
            if b < self.v0 or a > self.v1:
                continue
            r = QRectF(self._x(a), track.top() + 14, max(3, self._x(b) - self._x(a)), track_h - 28)
            sel = sid == self.selected
            p.setPen(QPen(QColor("white"), 2) if sel else Qt.NoPen)
            p.setBrush(QColor(62, 207, 142, 150 if sel else 95))
            p.drawRoundedRect(r, 4, 4)
            if r.width() > 40:
                p.setPen(QColor("white"))
                p.drawText(r.adjusted(5, 0, -3, 0), Qt.AlignVCenter | Qt.AlignLeft, lb or "")
        # in/out selection
        if self.in_pt is not None and self.out_pt is not None and self.out_pt > self.in_pt:
            xa, xb = self._x(self.in_pt), self._x(self.out_pt)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 79, 109, 55))
            p.drawRect(QRectF(xa, track.top(), xb - xa, track_h))
        for t, name in ((self.in_pt, "IN"), (self.out_pt, "OUT")):
            if t is None:
                continue
            x = self._x(t)
            p.setPen(QPen(QColor(theme.ACCENT), 2))
            p.drawLine(QPointF(x, track.top()), QPointF(x, track.bottom()))
            p.setBrush(QColor(theme.ACCENT))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(x - 14, track.bottom() - 2, 28, 12), 3, 3)
            p.setPen(QColor("white"))
            p.setFont(QFont(p.font().family(), 7, QFont.Bold))
            p.drawText(QRectF(x - 14, track.bottom() - 2, 28, 12), Qt.AlignCenter, name)
        # playhead
        x = self._x(self.position)
        p.setPen(QPen(QColor("white"), 2))
        p.drawLine(QPointF(x, ruler_h), QPointF(x, H - 2))
        p.setBrush(QColor("white"))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(x, ruler_h), 4, 4)

    # ---- mouse
    def _hit_handle(self, x):
        for name, t in (("in", self.in_pt), ("out", self.out_pt)):
            if t is not None and abs(self._x(t) - x) < 7:
                return name
        return None

    def mousePressEvent(self, e):
        x = e.position().x()
        h = self._hit_handle(x)
        if h:
            self._drag = h
            return
        t = self._t(x)
        y = e.position().y()
        if y > 44:
            for sid, a, b, _ in self.segments:
                if a <= t <= b:
                    self.selected = sid
                    self.segmentClicked.emit(sid)
                    self.update()
                    break
        self._drag = "seek"
        self.seek.emit(t)

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if self._drag is None:
            self.setCursor(Qt.SizeHorCursor if self._hit_handle(x) else Qt.PointingHandCursor)
            return
        t = self._t(x)
        if self._drag == "seek":
            self.seek.emit(t)
        elif self._drag == "in":
            self.in_pt = min(t, (self.out_pt or self.duration) - 0.1)
            self.inOutChanged.emit(self.in_pt, self.out_pt if self.out_pt is not None else -1)
            self.update()
        elif self._drag == "out":
            self.out_pt = max(t, (self.in_pt or 0) + 0.1)
            self.inOutChanged.emit(self.in_pt if self.in_pt is not None else -1, self.out_pt)
            self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def wheelEvent(self, e):
        dy = e.angleDelta().y() or e.angleDelta().x()
        span = self.v1 - self.v0
        if e.modifiers() & Qt.ShiftModifier or abs(e.angleDelta().x()) > abs(e.angleDelta().y()):
            shift = -dy / 1200 * span
            self.v0 = max(0.0, min(self.duration - span, self.v0 + shift))
            self.v1 = self.v0 + span
        else:
            factor = 0.8 if dy > 0 else 1.25
            new = max(2.0, min(self.duration, span * factor))
            anchor = self._t(e.position().x())
            k = (anchor - self.v0) / span if span else 0.5
            self.v0 = max(0.0, anchor - k * new)
            self.v1 = min(self.duration, self.v0 + new)
            self.v0 = max(0.0, self.v1 - new)
        self.update()

    def zoom_fit(self):
        self.v0, self.v1 = 0.0, self.duration
        self.update()
