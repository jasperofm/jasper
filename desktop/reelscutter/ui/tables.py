"""A light table model fed by SQL rows; refreshes in place so selection and scrolling survive."""
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from . import theme


class RowsModel(QAbstractTableModel):
    """columns: list of (header, fn(row) -> text). Optional progress_col shows a bar from row['progress'];
    status_col colours the text by status."""

    def __init__(self, columns, progress_col=None, status_col=None):
        super().__init__()
        self.columns = columns
        self.rows = []
        self.progress_col = progress_col
        self.status_col = status_col

    def set_rows(self, rows):
        new_ids = [r["id"] for r in rows]
        old_ids = [r["id"] for r in self.rows]
        if new_ids == old_ids:
            self.rows = rows
            if rows:
                self.dataChanged.emit(self.index(0, 0), self.index(len(rows) - 1, len(self.columns) - 1))
        else:
            self.beginResetModel()
            self.rows = rows
            self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.columns)

    def headerData(self, s, o, role=Qt.DisplayRole):
        if o == Qt.Horizontal and role == Qt.DisplayRole:
            return self.columns[s][0]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        c = index.column()
        if role == Qt.DisplayRole:
            try:
                return self.columns[c][1](r)
            except Exception:
                return ""
        if role == Qt.UserRole:
            if c == self.progress_col:
                return float(r["progress"] or 0)
            return r
        if role == Qt.ForegroundRole and c == self.status_col:
            return QColor(theme.STATUS_COLORS.get(r["status"], theme.TEXT))
        if role == Qt.ToolTipRole:
            try:
                return self.columns[c][1](r)
            except Exception:
                return None
        return None

    def row_at(self, index):
        return self.rows[index.row()]
