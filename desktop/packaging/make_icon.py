"""Draws the app icon (1024 px PNG). Run once; the PNG is committed and turned into .icns during the build."""
import sys
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

app = QGuiApplication(sys.argv)
S = 1024
img = QImage(S, S, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)
m = 100
bg = QPainterPath()
bg.addRoundedRect(QRectF(m, m, S - 2 * m, S - 2 * m), 185, 185)
g = QLinearGradient(m, m, S - m, S - m)
g.setColorAt(0, QColor("#1d222b"))
g.setColorAt(1, QColor("#0f1115"))
p.fillPath(bg, g)
# vertical "reel" frame
fr = QRectF(S * 0.355, S * 0.22, S * 0.29, S * 0.56)
acc = QLinearGradient(fr.topLeft(), fr.bottomRight())
acc.setColorAt(0, QColor("#ff4f6d"))
acc.setColorAt(1, QColor("#ff8a5c"))
path = QPainterPath()
path.addRoundedRect(fr, 46, 46)
p.fillPath(path, acc)
# play triangle
tri = QPainterPath()
cx, cy = S * 0.5 + 12, S * 0.5
tri.moveTo(cx - 52, cy - 68)
tri.lineTo(cx + 68, cy)
tri.lineTo(cx - 52, cy + 68)
tri.closeSubpath()
p.fillPath(tri, QColor("white"))
# cut marks either side
pen = QPen(QColor("#e8eaef"), 26, Qt.SolidLine, Qt.RoundCap)
p.setPen(pen)
for x in (S * 0.25, S * 0.75):
    p.drawLine(QPointF(x, S * 0.30), QPointF(x, S * 0.70))
p.end()
img.save(sys.argv[1] if len(sys.argv) > 1 else "icon.png")
