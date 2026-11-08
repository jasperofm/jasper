"""Dark theme (colours + Qt stylesheet)."""

BG = "#0f1115"
PANEL = "#161a21"
PANEL2 = "#1d222b"
BORDER = "#262c37"
TEXT = "#e8eaef"
MUTED = "#8a93a6"
ACCENT = "#ff4f6d"
ACCENT2 = "#ff8a5c"
GOOD = "#3ecf8e"
WARN = "#f5b84b"
BAD = "#ff5c5c"
INFO = "#5ca8ff"

STATUS_COLORS = {"done": GOOD, "failed": BAD, "running": INFO, "processing": INFO, "analyzing": INFO,
                 "queued": WARN, "new": MUTED, "canceled": MUTED, "missing": BAD, "paused": WARN}

QSS = f"""
* {{ font-family: -apple-system, "SF Pro Text", "Helvetica Neue", "Segoe UI", sans-serif; font-size: 13px; }}
QMainWindow, QDialog, QWidget#page {{ background: {BG}; color: {TEXT}; }}
QWidget {{ color: {TEXT}; }}
QLabel#h1 {{ font-size: 24px; font-weight: 700; }}
QLabel#h2 {{ font-size: 15px; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#statValue {{ font-size: 26px; font-weight: 700; }}
QLabel#statLabel {{ color: {MUTED}; font-size: 12px; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame#sidebar {{ background: {PANEL}; border-right: 1px solid {BORDER}; }}
QListWidget#nav {{ background: transparent; border: none; outline: 0; }}
QListWidget#nav::item {{ padding: 10px 14px; margin: 2px 8px; border-radius: 8px; color: {MUTED}; }}
QListWidget#nav::item:hover {{ background: {PANEL2}; color: {TEXT}; }}
QListWidget#nav::item:selected {{ background: {PANEL2}; color: {TEXT}; border-left: 3px solid {ACCENT}; }}
QPushButton {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 7px 14px; }}
QPushButton:hover {{ border-color: #3a4352; background: #232935; }}
QPushButton:pressed {{ background: #191d25; }}
QPushButton:disabled {{ color: #59606d; border-color: {BORDER}; }}
QPushButton#primary {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT}, stop:1 {ACCENT2});
    border: none; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: {ACCENT}; }}
QPushButton#primary:disabled {{ background: #5a2a33; color: #c9a3aa; }}
QPushButton#danger {{ color: {BAD}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 8px; selection-background-color: {ACCENT}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {PANEL2}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid #3a4352; background: {PANEL2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QTableView, QListView, QTreeView {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px;
    gridline-color: {BORDER}; selection-background-color: #2b3240; selection-color: {TEXT}; alternate-background-color: #181c24; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER}; padding: 6px; }}
QProgressBar {{ background: {PANEL2}; border: none; border-radius: 5px; height: 10px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ border-radius: 5px; background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT}, stop:1 {ACCENT2}); }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: white; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: #2c3340; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #2c3340; border-radius: 5px; min-width: 30px; }}
QToolTip {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; border-radius: 6px; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 8px 14px; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 10px; margin-top: 14px; padding: 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {MUTED}; }}
QMenu {{ background: {PANEL2}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
QSplitter::handle {{ background: {BG}; }}
"""
