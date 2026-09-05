# -*- coding: utf-8 -*-
"""全局 QSS 样式。"""

STYLE = """
QMainWindow, QWidget { background: #f2f4f8; color: #222; font-size: 13px; }
QFrame#card {
    background: #ffffff; border: 1px solid #e3e7ee; border-radius: 10px;
}
QLabel#appTitle { font-size: 18px; font-weight: bold; color: #1f6feb; }
QLabel#sectionTitle { font-size: 13px; font-weight: bold; color: #444; }
QLineEdit {
    background: #ffffff; border: 1px solid #ccd3e0; border-radius: 6px;
    padding: 5px 8px; selection-background-color: #1f6feb;
}
QLineEdit:focus { border: 1px solid #1f6feb; }
QPushButton {
    background: #e9edf5; border: 1px solid #ccd3e0; border-radius: 6px;
    padding: 5px 12px;
}
QPushButton:hover { background: #dfe6f2; }
QPushButton#primary { background: #1f6feb; color: white; border: none; }
QPushButton#primary:hover { background: #1a62d0; }
QPushButton#danger { background: #ffe4e4; color: #c0392b; border: 1px solid #f2c2c2; }
QProgressBar {
    background: #dfe5ef; border: none; border-radius: 4px; max-height: 8px;
}
QProgressBar::chunk { background: #1f6feb; border-radius: 4px; }
QPushButton#pause {
    background: #f2c97d; border: 1px solid #e0b158; border-radius: 6px;
    padding: 3px 8px; color: #5a3e00; font-weight: bold;
}
QPushButton#pause:hover { background: #eab85e; }
QPushButton#pause[paused="true"] {
    background: #1f6feb; border: none; color: white;
}
QPushButton#pause[paused="true"]:hover { background: #1a62d0; }
QCheckBox { spacing: 6px; }
QTableWidget {
    background: #ffffff; border: 1px solid #e3e7ee; border-radius: 8px;
    gridline-color: #eef1f6; alternate-background-color: #f7f9fc;
    selection-background-color: #e3ecfa; selection-color: #1f3a63;
}
QTableWidget::item { padding: 3px 6px; }
QHeaderView::section {
    background: #eef1f6; color: #4a5568; font-weight: bold;
    border: none; border-bottom: 1px solid #dfe4ec; padding: 6px 8px;
}
QFrame#statcard {
    background: #ffffff; border: 1px solid #e3e7ee; border-radius: 10px;
}
QPlainTextEdit {
    background: #1e2530; color: #d8e0ea; border-radius: 8px; padding: 6px;
    font-family: Consolas, monospace; font-size: 12px;
}
QScrollArea { border: none; }
QListWidget#settingsCat {
    background: #ffffff; border: 1px solid #e3e7ee; border-radius: 10px;
    padding: 6px; outline: none;
}
QListWidget#settingsCat::item {
    padding: 9px 12px; border-radius: 6px; margin: 1px 0;
    color: #3d4756;
}
QListWidget#settingsCat::item:hover { background: #f2f6fc; }
QListWidget#settingsCat::item:selected {
    background: #e3ecfa; color: #1f3a63; font-weight: bold;
}
QStackedWidget { background: transparent; }
"""


