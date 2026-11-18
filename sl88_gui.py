#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sl88_gui.py - PyQt5 editor for Studiologic SL88 Studio (Linux), designed after
the official SL Editor.
Place this file in the same directory as sl88_protocol.py and sl88_editor.py.
Installation (in your venv):
    source ~/sl88env/bin/activate
    pip install PyQt5
    python3 sl88_gui.py

Architecture:
* ALL MIDI communication goes through sl88_editor.SL88Editor and runs in ONE
  worker thread (MidiWorker, a QThread). The GUI thread never calls mido or
  SL88Editor directly.
* Results come back as pure data copies via Qt signals (queued between
  threads), so the window doesn't freeze during a config dump.
* print() from SL88Editor is captured (redirect_stdout) and displayed in the log.

Writable fields:
* Volume, velocity curve and STICK parameters are verified/intended.
* Channel, lowest/highest key are UNVERIFIED as writable fields and are
  therefore locked until "Allow unverified fields" is checked.
* USER curves (1-6) are editable and can be saved to the keyboard.

Theme: View -> Dark Mode (Ctrl+D). The choice is saved in ~/.sl88_gui_prefs.json.
"""
import contextlib
import json
import math
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

try:
    from PyQt5.QtCore import Qt, QThread, QRectF, QPointF, pyqtSignal
    from PyQt5.QtGui import (QPainter, QColor, QPen, QBrush, QFont, QPixmap,
                             QIcon, QLinearGradient, QPainterPath)
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton,
        QComboBox, QCheckBox, QLineEdit, QSlider, QTabWidget, QTreeWidget,
        QTreeWidgetItem, QTableWidget, QTableWidgetItem, QAbstractItemView,
        QHeaderView, QSplitter, QScrollArea, QDockWidget, QPlainTextEdit,
        QProgressBar, QMessageBox, QHBoxLayout, QVBoxLayout, QGridLayout,
        QAction, QSizePolicy, QFileDialog)
except ImportError:
    print("PyQt5 is missing. Install: pip install PyQt5 (in your venv)")
    sys.exit(1)

try:
    import mido
    import sl88_protocol as p
    from sl88_editor import SL88Editor
    import sl88_extras as ex
except ImportError as exc:
    print(f"Could not import MIDI modules: {exc}\n"
          "Make sure sl88_protocol.py, sl88_editor.py and sl88_extras.py are in the same "
          "directory and that mido + python-rtmidi are installed in your venv.")
    sys.exit(1)

# --- version guard -----------------------------------------------------------
_REQUIRED_P = ("NUM_STICKS", "STICK_CHOICES", "stick_field", "zone_offset",
               "stick_param_value", "stick_param_choice_from_value",
               "ZONE_FIELDS", "curve_type_value", "describe_curve_type")
_missing = [n for n in _REQUIRED_P if not hasattr(p, n)]
if _missing or not hasattr(SL88Editor, "set_zone_field"):
    print("sl88_gui.py requires newer sl88_protocol.py / sl88_editor.py.")
    print(f"  Missing symbols: {_missing or 'none'}")
    print("  Run: python3 sl88_editor.py --check")
    sys.exit(1)

CURVE_CHOICES = ["soft", "medium", "hard"] + [f"USER {n-3}" for n in p.USER_MEMORY_RANGE]
STICK_CHOICES = p.STICK_CHOICES
DUMP_TIMEOUT = 5.0
MAX_LOG_LINES = 2000
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
BLACK_PCS = (1, 3, 6, 8, 10)
ZONE_TABLE_ROWS = list(p.ZONE_FIELDS) + [f"pedal_{n}" for n in range(1, 5)]

ZONE_STYLES = {
    1: {"bg": "#f59b23", "deep": "#a8620a", "tint": "#fde3c0", "ink": "#2b1600"},
    2: {"bg": "#e0d81c", "deep": "#8f8905", "tint": "#f7f3c1", "ink": "#2b2900"},
    3: {"bg": "#8fd094", "deep": "#3f7f47", "tint": "#dcf0dd", "ink": "#0e2a12"},
    4: {"bg": "#74b4e2", "deep": "#2f6d99", "tint": "#d8e9f6", "ink": "#0d2438"},
}

PALETTES = {
    False: {"bg": "#b7bdc3", "panel": "#cfd4d9", "text": "#1a1d21",
            "field": "#f5f6f7", "border": "#7d848b", "accent": "#1e90d6",
            "button": "#e3e6e9", "button_hover": "#f1f3f4",
            "muted": "#7a8087", "header": "#aeb5bb"},
    True: {"bg": "#1d2024", "panel": "#272b31", "text": "#e4e7ea",
           "field": "#14161a", "border": "#454c55", "accent": "#29a3dc",
           "button": "#353a42", "button_hover": "#434952",
           "muted": "#7b828b", "header": "#31363d"},
}

QSS_TEMPLATE = """
QWidget { background-color: @bg@; color: @text@; }
QLabel { background: transparent; }
QLabel#led { background: #0d0e10; color: #eceff1; border: 1px solid #000000;
             border-radius: 2px; padding: 1px 6px; font-family: monospace;
             font-style: italic; font-weight: bold; }
QFrame#toolbar, QFrame#progstrip { background-color: @panel@;
                                   border: 1px solid @border@; border-radius: 4px; }
QPushButton { background-color: @button@; color: @text@;
              border: 1px solid @border@; border-radius: 3px; padding: 4px 12px; }
QPushButton:hover { background-color: @button_hover@; }
QPushButton:pressed { background-color: @accent@; color: #ffffff; }
QPushButton:disabled { color: @muted@; }
QComboBox, QLineEdit { background-color: @field@; color: @text@;
                       border: 1px solid @border@; border-radius: 3px; padding: 3px 6px;
                       selection-background-color: @accent@; selection-color: #ffffff; }
QComboBox:disabled, QLineEdit:disabled { color: @muted@; }
QComboBox QAbstractItemView { background-color: @field@; color: @text@;
                              selection-background-color: @accent@; selection-color: #ffffff;
                              border: 1px solid @border@; }
QCheckBox { background: transparent; spacing: 6px; }
QCheckBox:disabled { color: @muted@; }
QTabWidget::pane { border: 1px solid @border@; background-color: @panel@; top: -1px; }
QTabBar::tab { background-color: @button@; color: @text@; padding: 8px 22px;
               border: 1px solid @border@; border-bottom: none; font-weight: bold;
               margin-right: 2px; }
QTabBar::tab:selected { background-color: @panel@; color: @accent@;
                        border-top: 3px solid @accent@; }
QTabBar::tab:hover:!selected { background-color: @button_hover@; }
QTreeWidget, QTableWidget { background-color: @field@;
                            alternate-background-color: @panel@; color: @text@;
                            border: 1px solid @border@; gridline-color: @border@; }
QTreeWidget::item:selected, QTableWidget::item:selected {
    background-color: @accent@; color: #ffffff; }
QHeaderView::section { background-color: @header@; color: @text@;
                       border: 1px solid @border@; padding: 4px; font-weight: bold; }
QPlainTextEdit { background-color: @field@; color: @text@;
                 border: 1px solid @border@; font-family: monospace; font-size: 9pt; }
QScrollBar:vertical { background: @bg@; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: @border@; min-height: 24px; border-radius: 5px; }
QScrollBar::handle:vertical:hover { background: @accent@; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: @bg@; height: 12px; margin: 0; }
QScrollBar::handle:horizontal { background: @border@; min-width: 24px; border-radius: 5px; }
QScrollBar::handle:horizontal:hover { background: @accent@; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QStatusBar { background-color: @panel@; }
QProgressBar { border: 1px solid @border@; border-radius: 3px;
               background-color: @field@; text-align: center; max-height: 12px; }
QProgressBar::chunk { background-color: @accent@; }
QMenuBar { background-color: @panel@; }
QMenuBar::item:selected { background-color: @accent@; color: #ffffff; }
QMenu { background-color: @panel@; border: 1px solid @border@; }
QMenu::item:selected { background-color: @accent@; color: #ffffff; }
QDockWidget::title { background-color: @header@; padding: 4px; }
QSplitter::handle { background-color: @border@; }
QToolTip { background-color: @field@; color: @text@; border: 1px solid @border@; }
QScrollArea { border: none; }
"""

CARD_TEMPLATE = """
QFrame#zoneCard { background-color: @body@; border: 2px solid @border@; border-radius: 6px; }
QFrame#zoneCard QLabel { background: transparent; color: @text@; }
QFrame#zoneHead { background-color: @head@; border: none; border-radius: 3px; }
QFrame#zoneHead QLabel { background: transparent; color: @headtext@; }
QFrame#zoneCard QLabel#led { background-color: #0d0e10; color: #eceff1;
                             border: 1px solid #000000; border-radius: 2px; padding: 0 4px;
                             font-family: monospace; font-style: italic; font-weight: bold; }
QFrame#zoneCard QSlider::groove:vertical { background: rgba(0,0,0,110); width: 8px; border-radius: 4px; }
QFrame#zoneCard QSlider::handle:vertical { background: #f2efe6; border: 1px solid #1b1b1b;
                                           height: 16px; margin: 0 -7px; border-radius: 3px; }
QFrame#zoneCard QSlider::handle:vertical:disabled { background: #9a9a9a; }
QFrame#zoneCard QPushButton { background-color: @deep@; color: #ffffff;
                              border: 1px solid #000000; border-radius: 3px; padding: 3px 10px; font-weight: bold; }
QFrame#zoneCard QPushButton:hover { background-color: @ink@; }
QFrame#zoneCard QPushButton:disabled { background-color: rgba(0,0,0,70); color: rgba(255,255,255,120); }
"""


def fill_template(template, values):
    out = template
    for key, val in values.items():
        out = out.replace(f"@{key}@", val)
    return out


def build_qss(dark):
    return fill_template(QSS_TEMPLATE, PALETTES[bool(dark)])


def mix(a, b, t):
    ca, cb = QColor(a), QColor(b)
    return QColor(int(ca.red() * (1 - t) + cb.red() * t),
                  int(ca.green() * (1 - t) + cb.green() * t),
                  int(ca.blue() * (1 - t) + cb.blue() * t)).name()


def zone_sheet(zone, dark):
    st = ZONE_STYLES[zone]
    if dark:
        vals = {"body": mix("#23272d", st["bg"], 0.32), "head": st["bg"],
                "headtext": st["ink"], "text": "#eceff1", "border": st["bg"],
                "deep": st["deep"], "ink": st["ink"]}
    else:
        vals = {"body": st["bg"], "head": st["deep"], "headtext": "#ffffff",
                "text": st["ink"], "border": st["deep"],
                "deep": st["deep"], "ink": st["ink"]}
    return fill_template(CARD_TEMPLATE, vals)


def note_name(n):
    if not isinstance(n, int) or not 0 <= n <= 127:
        return str(n)
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def require(ed):
    if ed is None:
        raise RuntimeError("Not connected to SL88.")
    return ed


def snapshot(ed):
    return {
        "names": dict(ed.program_names),
        "curves": {k: dict(v) for k, v in ed.curves.items()},
        "active": ed.active_program,
        "dry_run": ed.dry_run,
        "groups": {k: dict(v) for k, v in getattr(ed, "groups", {}).items()},
    }


class GuiEditor(SL88Editor):
    def __init__(self, port_name, dry_run=False):
        self.groups = {}
        self._warned_group_len = False
        super().__init__(port_name, dry_run=dry_run)

    def _handle_message(self, data):
        super()._handle_message(data)
        if p.is_reply(data) and len(data) > 4 and data[4] == ex.GROUP_MSG:
            g = ex.parse_group(data)
            if g:
                self.groups[g["group"]] = g
            elif not self._warned_group_len:
                self._warned_group_len = True
                print(f"  (group reply (03) with length {len(data)} could not be parsed, "
                      f"expected {4 + ex.GROUP_BODY_LEN})")

    def send_raw(self, message):
        self._send(message)


def curve_slot(choice):
    if choice == "medium":
        return 0
    if choice == "soft":
        return 1
    if choice == "hard":
        return 2
    if choice and choice.startswith("memory"):
        try:
            return int(choice[6:]) - 1
        except ValueError:
            return None
    return None


def small_font(label, size=8, bold=True):
    f = QFont(label.font())
    f.setPointSize(size)
    f.setBold(bold)
    label.setFont(f)


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------
def joystick_pixmap(size, color):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    pt = QPainter(pm)
    pt.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)
    pen = QPen(c, max(1.5, size * 0.07))
    pen.setCapStyle(Qt.RoundCap)
    pt.setPen(pen)
    pt.setBrush(Qt.NoBrush)
    pt.drawEllipse(QRectF(size * 0.08, size * 0.30, size * 0.84, size * 0.62))
    pt.drawLine(QPointF(size * 0.5, size * 0.62), QPointF(size * 0.5, size * 0.26))
    pt.setBrush(QBrush(c))
    pt.drawEllipse(QPointF(size * 0.5, size * 0.2), size * 0.12, size * 0.12)
    pt.end()
    return pm


def make_app_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    pt = QPainter(pm)
    pt.setRenderHint(QPainter.Antialiasing)
    pt.setPen(Qt.NoPen)
    for i, z in enumerate((1, 2, 3, 4)):
        x, y = (i % 2) * 32, (i // 2) * 32
        pt.setBrush(QBrush(QColor(ZONE_STYLES[z]["bg"])))
        pt.drawRoundedRect(QRectF(x + 3, y + 3, 26, 26), 5, 5)
    pt.end()
    return QIcon(pm)


# ---------------------------------------------------------------------------
# Custom widgets
# ---------------------------------------------------------------------------
class KnobWidget(QWidget):
    valueChanged = pyqtSignal(int)

    def __init__(self, minimum=0, maximum=127, value=0, parent=None):
        super().__init__(parent)
        self._min, self._max = minimum, maximum
        self._value = max(minimum, min(maximum, value))
        self._press_y = None
        self._press_val = 0
        self._accent = QColor("#f59b23")
        self._track = QColor(0, 0, 0, 70)
        self.setFixedSize(54, 54)
        self.setCursor(Qt.SizeVerCursor)

    def value(self):
        return self._value

    def setRange(self, minimum, maximum):
        self._min, self._max = minimum, maximum
        self._value = max(minimum, min(maximum, self._value))
        self.update()

    def setValue(self, v):
        self._value = max(self._min, min(self._max, int(v)))
        self.update()

    def set_colors(self, accent, track):
        self._accent, self._track = QColor(accent), QColor(track)
        self.update()

    def _user_set(self, v):
        v = max(self._min, min(self._max, int(round(v))))
        if v != self._value:
            self._value = v
            self.update()
            self.valueChanged.emit(v)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press_y = e.y()
            self._press_val = self._value

    def mouseMoveEvent(self, e):
        if self._press_y is None or not (e.buttons() & Qt.LeftButton):
            return
        fine = 600.0 if e.modifiers() & Qt.ShiftModifier else 150.0
        delta = (self._press_y - e.y()) / fine * (self._max - self._min)
        self._user_set(self._press_val + delta)

    def mouseReleaseEvent(self, e):
        self._press_y = None

    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 120.0
        if steps:
            self._user_set(self._value + (1 if steps > 0 else -1) * max(1, abs(int(steps))))
        e.accept()

    def paintEvent(self, _e):
        p_ = QPainter(self)
        p_.setRenderHint(QPainter.Antialiasing)
        en = self.isEnabled()
        side = min(self.width(), self.height())
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2,
                      side, side).adjusted(3, 3, -3, -3)
        span = float(max(1, self._max - self._min))
        frac = (self._value - self._min) / span
        track = QPen(self._track, 3)
        track.setCapStyle(Qt.RoundCap)
        p_.setPen(track)
        p_.drawArc(rect, 225 * 16, -270 * 16)
        val = QPen(self._accent if en else QColor("#8a8a8a"), 3)
        val.setCapStyle(Qt.RoundCap)
        p_.setPen(val)
        p_.drawArc(rect, 225 * 16, int(-270 * 16 * frac))
        body = rect.adjusted(7, 7, -7, -7)
        grad = QLinearGradient(body.topLeft(), body.bottomRight())
        grad.setColorAt(0, QColor("#4a4d52" if en else "#55585c"))
        grad.setColorAt(1, QColor("#0e0f11" if en else "#2a2b2d"))
        p_.setPen(QPen(QColor("#000000"), 1))
        p_.setBrush(QBrush(grad))
        p_.drawEllipse(body)
        ang = math.radians(-135 + 270 * frac)
        c = body.center()
        r = body.width() / 2
        ptr = QPen(QColor("#f2efe6") if en else QColor("#8a8a8a"), 2.5)
        ptr.setCapStyle(Qt.RoundCap)
        p_.setPen(ptr)
        p_.drawLine(QPointF(c.x() + math.sin(ang) * r * 0.35, c.y() - math.cos(ang) * r * 0.35),
                    QPointF(c.x() + math.sin(ang) * r * 0.9, c.y() - math.cos(ang) * r * 0.9))


class KnobBox(QWidget):
    valueChanged = pyqtSignal(int)

    def __init__(self, title, minimum, maximum, fmt=str, parent=None):
        super().__init__(parent)
        self.fmt = fmt
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.disp = QLabel("–")
        self.disp.setObjectName("led")
        self.disp.setAlignment(Qt.AlignCenter)
        self.disp.setMinimumWidth(50)
        small_font(self.disp, 9)
        self.knob = KnobWidget(minimum, maximum)
        self.cap = QLabel(title)
        self.cap.setAlignment(Qt.AlignCenter)
        small_font(self.cap, 7)
        lay.addWidget(self.disp)
        lay.addWidget(self.knob, 0, Qt.AlignHCenter)
        lay.addWidget(self.cap)
        self.knob.valueChanged.connect(self._changed)

    def _changed(self, v):
        self.disp.setText(self.fmt(v))
        self.valueChanged.emit(v)

    def value(self):
        return self.knob.value()

    def set_value(self, v):
        v = int(v)
        if v > self.knob._max:
            self.knob.setRange(self.knob._min, v)
        self.knob.setValue(v)
        self.disp.setText(self.fmt(self.knob.value()))

    def clear(self):
        self.disp.setText("–")

    def set_colors(self, accent, track):
        self.knob.set_colors(accent, track)


# ---------------------------------------------------------------------------
# CurvePlot (read-only, used in zone cards)
# ---------------------------------------------------------------------------
class CurvePlot(QWidget):
    """Draws a velocity curve (table 0..127 or analytical fallback)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.table = None
        self.choice = "medium"
        self.setMinimumSize(40, 40)

    def set_curve(self, table, choice=None):
        self.table = list(table) if table else None
        self.choice = choice or "medium"
        self.update()

    def _points(self, n=64):
        t = self.table
        if t and len(t) > 0:
            max_val = float(max(127, max(t)))
            if max_val == 0:
                max_val = 127.0
            m = max(1, len(t) - 1)
            return [(i / m, min(1.0, max(0.0, t[i] / max_val))) for i in range(len(t))]
        f = {"soft": lambda x: 1 - (1 - x) ** 2,
             "hard": lambda x: x * x}.get(self.choice or "medium", lambda x: x)
        return [(i / n, f(i / n)) for i in range(n + 1)]

    def paintEvent(self, _e):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        pt.fillRect(r, QColor("#14161a"))
        pt.setPen(QPen(QColor(255, 255, 255, 28), 1))
        for k in (1, 2, 3):
            pt.drawLine(QPointF(r.left() + r.width() * k / 4, r.top()),
                        QPointF(r.left() + r.width() * k / 4, r.bottom()))
            pt.drawLine(QPointF(r.left(), r.top() + r.height() * k / 4),
                        QPointF(r.right(), r.top() + r.height() * k / 4))
        pt.setPen(QPen(QColor(255, 255, 255, 50), 1, Qt.DashLine))
        pt.drawLine(r.bottomLeft(), r.topRight())
        path = QPainterPath()
        for i, (x, y) in enumerate(self._points()):
            px, py = r.left() + x * r.width(), r.bottom() - y * r.height()
            if i == 0:
                path.moveTo(px, py)
            else:
                path.lineTo(px, py)
        pt.setPen(QPen(QColor("#f59b23"), 2))
        pt.setBrush(Qt.NoBrush)
        pt.drawPath(path)
        pt.setPen(QPen(QColor("#555a60"), 1))
        pt.drawRect(r)


# ---------------------------------------------------------------------------
# EditableCurvePlot (for USER curves in SL CURVES tab)
# ---------------------------------------------------------------------------
class EditableCurvePlot(QWidget):
    """Editable velocity curve. Drag the white points to change the curve."""
    curveChanged = pyqtSignal(list)

    NUM_POINTS = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.table = None
        self.choice = "medium"
        self.editable = False
        self._drag_point = None
        self._hover_point = None
        self.setMinimumSize(180, 180)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.PointingHandCursor)

    def set_curve(self, table, choice=None, editable=False):
        self.table = list(table) if table else None
        self.choice = choice or "medium"
        self.editable = editable
        
        # Om tabell finns men har annan längd, interpolera till NUM_POINTS (16)
        if self.table and len(self.table) > 0:
            if len(self.table) != self.NUM_POINTS:
                old_len = len(self.table)
                new_table = []
                for i in range(self.NUM_POINTS):
                    old_idx = i * (old_len - 1) / max(1, self.NUM_POINTS - 1)
                    idx_low = int(old_idx)
                    idx_high = min(idx_low + 1, old_len - 1)
                    frac = old_idx - idx_low
                    val = self.table[idx_low] * (1 - frac) + self.table[idx_high] * frac
                    new_table.append(int(min(127, max(0, val))))
                self.table = new_table
        else:
            # Standard linjär kurva om data saknas helt
            self.table = [int(i * 127 / (self.NUM_POINTS - 1)) for i in range(self.NUM_POINTS)]
        self.update()

    def get_table(self):
        return list(self.table) if self.table else None

    def _plot_rect(self):
        return QRectF(10, 10, max(20, self.width() - 20), max(20, self.height() - 20))

    def _point_pos(self, i, r):
        if not self.table:
            return None
        x = r.left() + i * r.width() / (self.NUM_POINTS - 1)
        y = r.bottom() - self.table[i] / 127.0 * r.height()
        return QPointF(x, y)

    def _nearest_point(self, pos, r):
        if not self.table:
            return None
        min_dist = float('inf')
        nearest = None
        for i in range(self.NUM_POINTS):
            p = self._point_pos(i, r)
            if p:
                dist = (p.x() - pos.x()) ** 2 + (p.y() - pos.y()) ** 2
                if dist < min_dist:
                    min_dist = dist
                    nearest = i
        # 15 pixel catch radius
        if min_dist < 225: 
            return nearest
        return None

    def _y_to_value(self, y, r):
        return int(max(0, min(127, (r.bottom() - y) / r.height() * 127)))

    def paintEvent(self, _e):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        r = self._plot_rect()

        # Background
        pt.fillRect(r, QColor("#14161a"))

        # Grid
        pt.setPen(QPen(QColor(255, 255, 255, 28), 1))
        for k in (1, 2, 3):
            pt.drawLine(QPointF(r.left() + r.width() * k / 4, r.top()),
                        QPointF(r.left() + r.width() * k / 4, r.bottom()))
            pt.drawLine(QPointF(r.left(), r.top() + r.height() * k / 4),
                        QPointF(r.right(), r.top() + r.height() * k / 4))

        # Diagonal reference line
        pt.setPen(QPen(QColor(255, 255, 255, 50), 1, Qt.DashLine))
        pt.drawLine(r.bottomLeft(), r.topRight())

        if not self.table:
            pt.setPen(QPen(QColor("#555a60"), 1))
            pt.drawRect(r)
            return

        # Draw the curve path
        path = QPainterPath()
        for i in range(self.NUM_POINTS):
            p = self._point_pos(i, r)
            if i == 0:
                path.moveTo(p)
            else:
                path.lineTo(p)

        pt.setPen(QPen(QColor("#f59b23"), 2))
        pt.setBrush(Qt.NoBrush)  # Ensure we don't fill the path
        pt.drawPath(path)

        # Draw control points (only if editable)
        if self.editable:
            for i in range(self.NUM_POINTS):
                p = self._point_pos(i, r)
                is_active = (i == self._drag_point)
                is_hover = (i == self._hover_point)
                
                rad = 7 if is_active else (6 if is_hover else 5)
                
                # White points with black border for high visibility
                pt.setPen(QPen(QColor("#000000"), 1.5))
                pt.setBrush(QBrush(QColor("#ffffff")))
                pt.drawEllipse(p, rad, rad)

        # Frame
        pt.setPen(QPen(QColor("#555a60"), 1))
        pt.setBrush(Qt.NoBrush)
        pt.drawRect(r)

    def mousePressEvent(self, e):
        if not self.editable:
            return
        r = self._plot_rect()
        if e.button() == Qt.LeftButton:
            self._drag_point = self._nearest_point(e.pos(), r)
            if self._drag_point is not None:
                self._update_point(e.pos(), r)

    def mouseMoveEvent(self, e):
        if not self.editable:
            return
        r = self._plot_rect()
        if self._drag_point is not None and (e.buttons() & Qt.LeftButton):
            self._update_point(e.pos(), r)
        else:
            self._hover_point = self._nearest_point(e.pos(), r)
            self.update()

    def mouseReleaseEvent(self, _e):
        if self._drag_point is not None:
            self._drag_point = None
            if self.table:
                self.curveChanged.emit(self.table)

    def mouseDoubleClickEvent(self, e):
        if not self.editable:
            return
        r = self._plot_rect()
        idx = self._nearest_point(e.pos(), r)
        if idx is not None and self.table:
            # Reset point to linear position
            self.table[idx] = int(idx * 127 / (self.NUM_POINTS - 1))
            self.update()
            self.curveChanged.emit(self.table)

    def _update_point(self, pos, r):
        if self._drag_point is not None and self.table:
            self.table[self._drag_point] = self._y_to_value(pos.y(), r)
            self.update()

    def leaveEvent(self, _e):
        self._hover_point = None
        self.update()


# ---------------------------------------------------------------------------
# Keyboard widget
# ---------------------------------------------------------------------------
class KeyboardWidget(QWidget):
    LOW, HIGH = 21, 108

    def __init__(self, parent=None):
        super().__init__(parent)
        self.zones = None
        self.dark = False
        self.setFixedHeight(96)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_zones(self, zones):
        self.zones = zones
        self.update()

    def set_dark(self, dark):
        self.dark = dark
        self.update()

    def _paint_key(self, pt, rect, n, base, black):
        pt.fillRect(rect, base)
        if self.zones:
            qh = rect.height() / 4.0
            for z, (lo, hi, en) in enumerate(self.zones):
                if isinstance(lo, int) and isinstance(hi, int) and lo <= n <= hi:
                    col = QColor(ZONE_STYLES[z + 1]["bg"])
                    if black:
                        col = col.darker(160)
                    if not en:
                        col.setAlpha(90)
                    pt.fillRect(QRectF(rect.left(), rect.top() + z * qh, rect.width(), qh), col)
        pt.setPen(QPen(QColor("#2a2d31"), 1))
        pt.setBrush(Qt.NoBrush)
        pt.drawRect(rect)

    def paintEvent(self, _e):
        pt = QPainter(self)
        w, h = self.width(), self.height()
        pt.fillRect(self.rect(), QColor("#101214" if self.dark else "#4a4f55"))
        whites = [n for n in range(self.LOW, self.HIGH + 1) if n % 12 not in BLACK_PCS]
        x0, y0 = 4.0, 4.0
        kw = (w - 8.0) / len(whites)
        kh = h - 8.0
        widx = {n: i for i, n in enumerate(whites)}
        white_base = QColor("#d8dadc" if self.dark else "#f6f6f4")
        for n in whites:
            rect = QRectF(x0 + widx[n] * kw, y0, kw, kh)
            self._paint_key(pt, rect, n, white_base, False)
        bw, bh = kw * 0.62, kh * 0.62
        for n in range(self.LOW, self.HIGH + 1):
            if n % 12 in BLACK_PCS and (n - 1) in widx:
                cx = x0 + (widx[n - 1] + 1) * kw
                self._paint_key(pt, QRectF(cx - bw / 2, y0, bw, bh), n, QColor("#1a1a1a"), True)


# ---------------------------------------------------------------------------
# Bridge between worker thread and GUI
# ---------------------------------------------------------------------------
class SignalWriter:
    def __init__(self, emit):
        self.emit = emit
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.emit(line)
        return len(s)

    def flush(self):
        if self.buf.strip():
            self.emit(self.buf)
        self.buf = ""

    def isatty(self):
        return False


class Job:
    __slots__ = ("fn", "on_done", "label", "key")

    def __init__(self, fn, on_done=None, label="Working", key=None):
        self.fn, self.on_done, self.label, self.key = fn, on_done, label, key


class MidiWorker(QThread):
    log = pyqtSignal(str)
    status = pyqtSignal(str)
    done = pyqtSignal(object, object)
    failed = pyqtSignal(object, str)
    cancelled = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.jobs = queue.Queue()
        self.editor = None
        self._stopping = threading.Event()

    def submit(self, job):
        self.jobs.put(job)

    def stop(self):
        self._stopping.set()

    def close_editor(self):
        if self.editor is not None:
            try:
                self.editor.close()
            except Exception:
                pass
        self.editor = None

    def _drop_stale(self, job):
        if job.key is None:
            return job
        rest = []
        while True:
            try:
                rest.append(self.jobs.get_nowait())
            except queue.Empty:
                break
        newer = any(j.key == job.key for j in rest)
        for j in rest:
            self.jobs.put(j)
        if newer:
            self.cancelled.emit(job)
            return None
        return job

    def _run_job(self, job):
        self.status.emit(job.label + " …")
        writer = SignalWriter(self.log.emit)
        try:
            with contextlib.redirect_stdout(writer):
                result = job.fn(self.editor)
        except Exception as exc:
            writer.flush()
            self.failed.emit(job, f"{type(exc).__name__}: {exc}")
            if not isinstance(exc, (ValueError, RuntimeError)):
                self.log.emit(traceback.format_exc().rstrip())
        else:
            writer.flush()
            self.done.emit(job, result)

    def run(self):
        while not self._stopping.is_set():
            try:
                job = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            job = self._drop_stale(job)
            if job is not None:
                self._run_job(job)
        self.close_editor()


# ---------------------------------------------------------------------------
# Zone card (PROGRAM tab)
# ---------------------------------------------------------------------------
class ZoneCard(QFrame):
    applyRequested = pyqtSignal(int, dict)

    def __init__(self, zone, parent=None):
        super().__init__(parent)
        self.zone = zone
        self.dirty = set()
        self.dark = False
        self.setObjectName("zoneCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        head = QFrame()
        head.setObjectName("zoneHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(8, 3, 8, 3)
        self.badge = QLabel(str(zone))
        f = QFont(self.badge.font())
        f.setPointSize(16)
        f.setBold(True)
        self.badge.setFont(f)
        title = QLabel("INSTRUMENT SOUND")
        small_font(title, 11)
        self.enabled_lbl = QLabel("–")
        small_font(self.enabled_lbl, 9)
        self.range_lbl = QLabel("–")
        small_font(self.range_lbl, 9)
        self.apply_btn = QPushButton("Apply Zone")
        self.apply_btn.clicked.connect(self._apply)
        hl.addWidget(self.badge)
        hl.addWidget(title)
        hl.addStretch(1)
        hl.addWidget(self.enabled_lbl)
        hl.addSpacing(8)
        hl.addWidget(self.range_lbl)
        hl.addSpacing(8)
        hl.addWidget(self.apply_btn)
        root.addWidget(head)

        body = QHBoxLayout()
        body.setSpacing(10)
        root.addLayout(body, 1)

        vcol = QVBoxLayout()
        self.vol_led = QLabel("–")
        self.vol_led.setObjectName("led")
        self.vol_led.setAlignment(Qt.AlignCenter)
        small_font(self.vol_led, 10)
        self.vol_slider = QSlider(Qt.Vertical)
        self.vol_slider.setRange(0, 127)
        self.vol_slider.setMinimumHeight(110)
        self.vol_slider.valueChanged.connect(self._on_volume)
        vcap = QLabel("VOLUME")
        vcap.setAlignment(Qt.AlignCenter)
        small_font(vcap, 7)
        vcol.addWidget(self.vol_led)
        vcol.addWidget(self.vol_slider, 1, Qt.AlignHCenter)
        vcol.addWidget(vcap)
        body.addLayout(vcol)

        ccol = QVBoxLayout()
        crow = QHBoxLayout()
        self.curve_plot = CurvePlot()
        self.curve_plot.setFixedSize(56, 56)
        crow.addWidget(self.curve_plot)
        ckv = QVBoxLayout()
        ccap = QLabel("VELOCITY CURVE")
        small_font(ccap, 7)
        self.curve_cb = QComboBox()
        self.curve_cb.addItems(CURVE_CHOICES)
        self.curve_cb.setCurrentIndex(-1)
        self.curve_cb.activated.connect(self._on_curve)
        self.curve_info = QLabel("")
        small_font(self.curve_info, 7, False)
        ckv.addWidget(ccap)
        ckv.addWidget(self.curve_cb)
        ckv.addWidget(self.curve_info)
        crow.addLayout(ckv, 1)
        ccol.addLayout(crow)

        krow = QHBoxLayout()
        self.ch_box = KnobBox("CHANNEL*", 1, 16)
        self.lo_box = KnobBox("LOWEST*", 0, 127, note_name)
        self.hi_box = KnobBox("HIGHEST*", 0, 127, note_name)
        for field, box in (("midi_channel", self.ch_box), ("low_key", self.lo_box),
                           ("high_key", self.hi_box)):
            box.setToolTip("Unverified writable field – requires 'Allow unverified fields'.")
            box.valueChanged.connect(lambda _v, f=field: self.dirty.add(f))
            krow.addWidget(box)
        ccol.addLayout(krow)
        prow = QHBoxLayout()
        pcap = QLabel("MIDI PORT")
        small_font(pcap, 7)
        self.port_cb = QComboBox()
        self.port_cb.addItems(["USB", "MIDI OUT 1", "MIDI OUT 2"])
        self.port_cb.setCurrentIndex(-1)
        self.port_cb.activated.connect(lambda _i: self.dirty.add("midi_port"))
        prow.addWidget(pcap)
        prow.addWidget(self.port_cb, 1)
        ccol.addLayout(prow)
        self.en_chk = QCheckBox("ENABLE")
        self.en_chk.clicked.connect(lambda _c: self.dirty.add("enabled"))
        ccol.addWidget(self.en_chk)
        ccol.addStretch(1)
        body.addLayout(ccol, 3)

        scol = QVBoxLayout()
        self.stick_icon = QLabel()
        self.stick_icon.setAlignment(Qt.AlignCenter)
        scol.addWidget(self.stick_icon)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(1)
        self.stick_lbls = {}
        row = 0
        for s in range(1, p.NUM_STICKS + 1):
            for axis in ("x", "y"):
                name = QLabel(f"STICK {s} {axis.upper()}")
                small_font(name, 7)
                val = QLabel("–")
                val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                small_font(val, 7)
                grid.addWidget(name, row, 0)
                grid.addWidget(val, row, 1)
                self.stick_lbls[(s, axis)] = val
                row += 1
        scol.addLayout(grid)

        pgrid = QGridLayout()
        pgrid.setHorizontalSpacing(8)
        pgrid.setVerticalSpacing(1)
        self.pedal_lbls = {}
        for n in range(1, ex.NUM_PEDALS + 1):
            pname = QLabel(f"PEDAL {n}")
            small_font(pname, 7)
            pval = QLabel("–")
            pval.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            small_font(pval, 7)
            pgrid.addWidget(pname, n - 1, 0)
            pgrid.addWidget(pval, n - 1, 1)
            self.pedal_lbls[n] = pval
        scol.addSpacing(4)
        scol.addLayout(pgrid)
        scol.addStretch(1)
        body.addLayout(scol, 2)

        self.apply_theme(False)
        self.set_editable(False, False)

    def apply_theme(self, dark):
        self.dark = dark
        self.setStyleSheet(zone_sheet(self.zone, dark))
        st = ZONE_STYLES[self.zone]
        accent = st["tint"] if not dark else st["bg"]
        track = QColor(0, 0, 0, 70) if not dark else QColor(255, 255, 255, 40)
        for box in (self.ch_box, self.lo_box, self.hi_box):
            box.set_colors(accent, track)
        self.stick_icon.setPixmap(joystick_pixmap(40, st["ink"] if not dark else st["bg"]))

    def set_editable(self, flag, advanced):
        self.vol_slider.setEnabled(flag)
        self.curve_cb.setEnabled(flag)
        self.apply_btn.setEnabled(flag)
        self.port_cb.setEnabled(flag)
        self.en_chk.setEnabled(flag)
        for box in (self.ch_box, self.lo_box, self.hi_box):
            box.setEnabled(flag and advanced)

    def clear(self):
        self.dirty.clear()
        self.vol_slider.blockSignals(True)
        self.vol_slider.setValue(0)
        self.vol_slider.blockSignals(False)
        self.vol_led.setText("–")
        self.curve_cb.setCurrentIndex(-1)
        self.curve_info.setText("")
        self.curve_plot.set_curve(None, "medium")
        self.port_cb.setCurrentIndex(-1)
        for box in (self.ch_box, self.lo_box, self.hi_box):
            box.clear()
        self.enabled_lbl.setText("–")
        self.en_chk.setChecked(False)
        self.range_lbl.setText("–")
        for lbl in self.stick_lbls.values():
            lbl.setText("–")
        for lbl in self.pedal_lbls.values():
            lbl.setText("–")

    def load(self, zd, curves, pedals=None):
        self.dirty.clear()
        vol = zd["volume"]
        self.vol_slider.blockSignals(True)
        self.vol_slider.setValue(min(127, vol))
        self.vol_slider.blockSignals(False)
        self.vol_led.setText(str(vol))
        self.ch_box.set_value(zd["midi_channel"] + 1)
        self.lo_box.set_value(zd["low_key"])
        self.hi_box.set_value(zd["high_key"])
        mp = zd["midi_port"]
        self.port_cb.setCurrentIndex(mp if 0 <= mp <= 2 else -1)
        self.en_chk.setChecked(zd["enabled"] != 0)
        self.enabled_lbl.setText("ON" if zd["enabled"] else "OFF")
        self.range_lbl.setText(f"{note_name(zd['low_key'])} – {note_name(zd['high_key'])}")
        choice = p.curve_choice_from_type(zd["curve_type"])
        idx = self.curve_cb.findText(choice) if choice else -1
        self.curve_cb.setCurrentIndex(idx)
        self.curve_info.setText("" if choice else
                                f"{p.describe_curve_type(zd['curve_type'])} (0x{zd['curve_type']:02X})")
        self._set_plot(choice, curves)
        for s in range(1, p.NUM_STICKS + 1):
            for axis in ("x", "y"):
                v = zd.get(p.stick_field(s, axis), 0)
                self.stick_lbls[(s, axis)].setText(p.describe_stick_value(v).upper())
        for n in range(1, ex.NUM_PEDALS + 1):
            lbl = self.pedal_lbls[n]
            if pedals is None:
                lbl.setText("–")
            else:
                lbl.setText(ex.describe_pedal_value(pedals[n - 1]).upper())

    def _set_plot(self, choice, curves):
        slot = curve_slot(choice)
        c = curves.get(slot) if (curves and slot is not None) else None
        self.curve_plot.set_curve(c["table"] if c else None, choice)

    def _on_volume(self, v):
        self.vol_led.setText(str(v))
        self.dirty.add("volume")

    def _on_curve(self, _idx):
        self.dirty.add("curve")
        self.curve_plot.set_curve(None, self.curve_cb.currentText())

    def _apply(self):
        changes = {}
        for f in self.dirty:
            if f == "volume":
                changes[f] = self.vol_slider.value()
            elif f == "curve":
                changes[f] = self.curve_cb.currentText()
            elif f == "midi_channel":
                changes[f] = self.ch_box.value()
            elif f == "low_key":
                changes[f] = self.lo_box.value()
            elif f == "high_key":
                changes[f] = self.hi_box.value()
            elif f == "midi_port":
                changes[f] = self.port_cb.currentIndex()
            elif f == "enabled":
                changes[f] = 2 if self.en_chk.isChecked() else 0
        self.applyRequested.emit(self.zone, changes)


# ---------------------------------------------------------------------------
# STICKS tab
# ---------------------------------------------------------------------------
class StickZonePanel(QFrame):
    applyStick = pyqtSignal(int, int)
    applyZone = pyqtSignal(int)

    def __init__(self, zone, parent=None):
        super().__init__(parent)
        self.zone = zone
        self.setObjectName("zoneCard")
        self.combos = {}
        self.raw = {}
        self.icons = []
        self.buttons = []
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(4)

        head = QFrame()
        head.setObjectName("zoneHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(8, 3, 8, 3)
        badge = QLabel(str(zone))
        small_font(badge, 14)
        title = QLabel(f"ZONE {zone}")
        small_font(title, 10)
        self.range_lbl = QLabel("–")
        small_font(self.range_lbl, 9)
        self.zone_btn = QPushButton("Apply Zone")
        self.zone_btn.clicked.connect(lambda _=False: self.applyZone.emit(self.zone))
        self.buttons.append(self.zone_btn)
        hl.addWidget(badge)
        hl.addWidget(title)
        hl.addSpacing(10)
        hl.addWidget(self.range_lbl)
        hl.addStretch(1)
        hl.addWidget(self.zone_btn)
        root.addWidget(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        for c, txt in ((1, "STICK"), (2, "X parameter"), (3, "Y parameter"), (4, "Raw value")):
            lbl = QLabel(txt)
            small_font(lbl, 8)
            grid.addWidget(lbl, 0, c)

        for s in range(1, p.NUM_STICKS + 1):
            icon = QLabel()
            self.icons.append(icon)
            grid.addWidget(icon, s, 0)
            name = QLabel(f"STICK {s}")
            small_font(name, 9)
            grid.addWidget(name, s, 1)
            for col, axis in ((2, "x"), (3, "y")):
                cb = QComboBox()
                cb.addItems(STICK_CHOICES)
                cb.setCurrentIndex(-1)
                cb.setMinimumContentsLength(18)
                cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLength)
                cb.setMaxVisibleItems(20)
                self.combos[(s, axis)] = cb
                grid.addWidget(cb, s, col)
            raw = QLabel("–")
            small_font(raw, 8, False)
            self.raw[s] = raw
            grid.addWidget(raw, s, 4)
            btn = QPushButton("Apply")
            btn.clicked.connect(lambda _=False, s=s: self.applyStick.emit(self.zone, s))
            self.buttons.append(btn)
            grid.addWidget(btn, s, 5)
        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 1)
        root.addLayout(grid)

        self.apply_theme(False)
        self.set_editable(False)

    def apply_theme(self, dark):
        self.setStyleSheet(zone_sheet(self.zone, dark))
        st = ZONE_STYLES[self.zone]
        pm = joystick_pixmap(30, st["ink"] if not dark else st["bg"])
        for icon in self.icons:
            icon.setPixmap(pm)

    def set_editable(self, flag):
        for w in list(self.combos.values()) + self.buttons:
            w.setEnabled(flag)

    def clear(self):
        self.range_lbl.setText("–")
        for cb in self.combos.values():
            cb.setCurrentIndex(-1)
        for lbl in self.raw.values():
            lbl.setText("–")

    def load(self, zd, log=None):
        self.range_lbl.setText(f"{note_name(zd['low_key'])} – {note_name(zd['high_key'])}")
        for s in range(1, p.NUM_STICKS + 1):
            xv = zd.get(p.stick_field(s, "x"), 0)
            yv = zd.get(p.stick_field(s, "y"), 0)
            for axis, val in (("x", xv), ("y", yv)):
                choice = p.stick_param_choice_from_value(val)
                cb = self.combos[(s, axis)]
                cb.setCurrentIndex(cb.findText(choice) if choice else -1)
                if choice is None and log:
                    log(f"Zone {self.zone} stick {s} {axis.upper()}: unknown value "
                        f"{val} – select parameter manually.")
            self.raw[s].setText(f"X {xv} · Y {yv}")

    def collect(self, sticks):
        out = []
        for s in sticks:
            xv = self.combos[(s, "x")].currentText()
            yv = self.combos[(s, "y")].currentText()
            if not xv or not yv:
                QMessageBox.information(self, "SL88",
                                        f"Select both X and Y parameters for STICK {s} in zone {self.zone}.")
                return None
            try:
                out.append((s, p.stick_param_value(xv), p.stick_param_value(yv)))
            except ValueError as exc:
                QMessageBox.critical(self, "SL88", str(exc))
                return None
        return out


# ---------------------------------------------------------------------------
# PEDALS tab
# ---------------------------------------------------------------------------
class PedalZonePanel(QFrame):
    applyPedal = pyqtSignal(int, int)
    applyZone = pyqtSignal(int)

    def __init__(self, zone, parent=None):
        super().__init__(parent)
        self.zone = zone
        self.setObjectName("zoneCard")
        self.combos = {}
        self.raw = {}
        self.buttons = []
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(4)

        head = QFrame()
        head.setObjectName("zoneHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(8, 3, 8, 3)
        badge = QLabel(str(zone))
        small_font(badge, 14)
        title = QLabel(f"ZONE {zone} · PEDALS")
        small_font(title, 10)
        self.zone_btn = QPushButton("Apply Zone")
        self.zone_btn.clicked.connect(lambda _=False: self.applyZone.emit(self.zone))
        self.buttons.append(self.zone_btn)
        hl.addWidget(badge)
        hl.addWidget(title)
        hl.addStretch(1)
        hl.addWidget(self.zone_btn)
        root.addWidget(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        for c, txt in ((0, "PEDAL"), (1, "Function"), (2, "Raw value")):
            lbl = QLabel(txt)
            small_font(lbl, 8)
            grid.addWidget(lbl, 0, c)

        for n in range(1, ex.NUM_PEDALS + 1):
            name = QLabel(f"PEDAL {n}")
            small_font(name, 9)
            grid.addWidget(name, n, 0)
            cb = QComboBox()
            cb.addItems(ex.PEDAL_CHOICES)
            cb.setCurrentIndex(-1)
            cb.setMinimumContentsLength(20)
            cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLength)
            cb.setMaxVisibleItems(20)
            self.combos[n] = cb
            grid.addWidget(cb, n, 1)
            raw = QLabel("–")
            small_font(raw, 8, False)
            self.raw[n] = raw
            grid.addWidget(raw, n, 2)
            btn = QPushButton("Apply")
            btn.clicked.connect(lambda _=False, n=n: self.applyPedal.emit(self.zone, n))
            self.buttons.append(btn)
            grid.addWidget(btn, n, 3)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        self.apply_theme(False)
        self.set_editable(False)

    def apply_theme(self, dark):
        self.setStyleSheet(zone_sheet(self.zone, dark))

    def set_editable(self, flag):
        for w in list(self.combos.values()) + self.buttons:
            w.setEnabled(flag)

    def clear(self):
        for cb in self.combos.values():
            cb.setCurrentIndex(-1)
        for lbl in self.raw.values():
            lbl.setText("–")

    def load(self, array, log=None):
        for n in range(1, ex.NUM_PEDALS + 1):
            v = array[ex.pedal_offset(n, self.zone)]
            choice = ex.pedal_choice_from_value(v)
            cb = self.combos[n]
            cb.setCurrentIndex(cb.findText(choice) if choice else -1)
            self.raw[n].setText(f"raw value {v}")
            if choice is None and log:
                log(f"Zone {self.zone} pedal {n}: unknown value {v} – select function manually.")

    def collect(self, pedals):
        out = []
        for n in pedals:
            choice = self.combos[n].currentText()
            if not choice:
                QMessageBox.information(self, "SL88",
                                        f"Select function for PEDAL {n} in zone {self.zone}.")
                return None
            try:
                out.append((n, ex.pedal_value(choice)))
            except ValueError as exc:
                QMessageBox.critical(self, "SL88", str(exc))
                return None
        return out


# ---------------------------------------------------------------------------
# Key-balance editor
# ---------------------------------------------------------------------------
class KeyBalanceEditor(QWidget):
    changed = pyqtSignal()
    N = ex.KEY_COUNT
    LIM = ex.KB_MAX_PCT
    NODE_KEYS = [round(i * (ex.KEY_COUNT - 1) / 11.0) for i in range(12)]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.values = [0.0] * self.N
        self.node_vals = [0.0] * len(self.NODE_KEYS)
        self.mode = "points"
        self._drag_node = None
        self._last_idx = None
        self._hover = None
        self.setMinimumHeight(240)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def get_values(self):
        return list(self.values)

    def set_values(self, vals):
        if len(vals) != self.N:
            raise ValueError(f"key-balance requires {self.N} values")
        self.values = [max(-self.LIM, min(self.LIM, round(float(v), 1))) for v in vals]
        self.node_vals = [self.values[k] for k in self.NODE_KEYS]
        self.update()
        self.changed.emit()

    def set_mode(self, mode):
        self.mode = mode
        if mode == "points":
            self.node_vals = [self.values[k] for k in self.NODE_KEYS]
            self._rebuild()
        self.changed.emit()
        self.update()

    def _rebuild(self):
        keys = self.NODE_KEYS
        for i in range(self.N):
            j = 0
            while j < len(keys) - 2 and i > keys[j + 1]:
                j += 1
            k0, k1 = keys[j], keys[j + 1]
            t = 0.0 if k1 == k0 else (i - k0) / float(k1 - k0)
            t = max(0.0, min(1.0, t))
            s = (1 - math.cos(math.pi * t)) / 2
            self.values[i] = round(self.node_vals[j] * (1 - s) + self.node_vals[j + 1] * s, 1)

    def _plot_rect(self):
        return QRectF(6, 22, max(10, self.width() - 12), max(10, self.height() - 32))

    def _key_x(self, i, r):
        return r.left() + (i + 0.5) * r.width() / self.N

    def _pct_to_y(self, pct, r):
        return r.center().y() - pct / self.LIM * (r.height() / 2)

    def _y_to_pct(self, y, r):
        pct = (r.center().y() - y) / (r.height() / 2) * self.LIM
        return round(max(-self.LIM, min(self.LIM, pct)), 1)

    def _idx_at(self, x, r):
        i = int((x - r.left()) / (r.width() / self.N))
        return max(0, min(self.N - 1, i))

    def _nearest_node(self, x, r):
        return min(range(len(self.NODE_KEYS)),
                   key=lambda n: abs(x - self._key_x(self.NODE_KEYS[n], r)))

    def paintEvent(self, _e):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        r = self._plot_rect()
        mid = r.center().y()
        pt.fillRect(r, QColor("#14161a"))
        small = QFont(self.font())
        small.setPointSize(7)
        pt.setFont(small)
        for pct in (-30, -15, 0, 15, 30):
            y = self._pct_to_y(pct, r)
            pt.setPen(QPen(QColor(255, 255, 255, 70 if pct == 0 else 28), 1))
            pt.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            pt.setPen(QColor("#8a9097"))
            pt.drawText(QRectF(r.left() + 3, y - 12, 40, 12),
                        Qt.AlignLeft | Qt.AlignVCenter, f"{pct:+d}%")
        kw = r.width() / self.N
        for i, v in enumerate(self.values):
            col = QColor("#9a6a2a" if ex.is_black_key(i) else "#e9e9e6")
            y = self._pct_to_y(v, r)
            if abs(y - mid) < 1.5:
                bar = QRectF(r.left() + i * kw + kw * 0.1, mid - 0.75, kw * 0.8, 1.5)
            else:
                bar = QRectF(r.left() + i * kw + kw * 0.1, min(y, mid), kw * 0.8, abs(y - mid))
            pt.fillRect(bar, col)
        pt.setPen(QColor("#8a9097"))
        for i in range(self.N):
            note = ex.KEY_FIRST_NOTE + i
            if note % 12 == 0:
                pt.drawText(QRectF(r.left() + i * kw - 10, r.bottom() + 2, kw + 20, 10),
                            Qt.AlignHCenter | Qt.AlignTop, p.note_name(note))
        if self.mode == "points":
            path = QPainterPath()
            for i, v in enumerate(self.values):
                px, py = self._key_x(i, r), self._pct_to_y(v, r)
                if i == 0:
                    path.moveTo(px, py)
                else:
                    path.lineTo(px, py)
            pt.setPen(QPen(QColor("#f59b23"), 1.5))
            pt.setBrush(Qt.NoBrush)
            pt.drawPath(path)
            for n, (k, v) in enumerate(zip(self.NODE_KEYS, self.node_vals)):
                active = (n == self._drag_node)
                rad = 7 if active else 5.5
                pt.setPen(QPen(QColor("#000000"), 1))
                pt.setBrush(QBrush(QColor("#ffc069" if active else "#f59b23")))
                pt.drawEllipse(QPointF(self._key_x(k, r), self._pct_to_y(v, r)), rad, rad)
        if self._hover is not None:
            big = QFont(self.font())
            big.setPointSize(9)
            big.setBold(True)
            pt.setFont(big)
            pt.setPen(QColor("#eceff1"))
            h = self._hover
            pt.drawText(QRectF(r.left(), 2, r.width(), 18), Qt.AlignHCenter | Qt.AlignVCenter,
                        f"{ex.key_label(h)}   {self.values[h]:+.1f}%")

    def _move_node(self, y, r):
        if self._drag_node is None:
            return
        self.node_vals[self._drag_node] = self._y_to_pct(y, r)
        self._rebuild()
        self.update()
        self.changed.emit()

    def _paint_at(self, x, y, r, zero):
        idx = self._idx_at(x, r)
        pct = 0.0 if zero else self._y_to_pct(y, r)
        if self._last_idx is None or self._last_idx == idx:
            self.values[idx] = pct
        else:
            a, b = self._last_idx, idx
            pa = self.values[a]
            step = 1 if b > a else -1
            for k in range(a, b + step, step):
                self.values[k] = round(pa + (pct - pa) * (k - a) / float(b - a), 1)
        self._last_idx = idx
        self.update()
        self.changed.emit()

    def mousePressEvent(self, e):
        r = self._plot_rect()
        if self.mode == "points":
            if e.button() == Qt.LeftButton:
                self._drag_node = self._nearest_node(e.x(), r)
                self._move_node(e.y(), r)
        elif e.button() in (Qt.LeftButton, Qt.RightButton):
            self._last_idx = None
            self._paint_at(e.x(), e.y(), r, e.button() == Qt.RightButton)

    def mouseMoveEvent(self, e):
        r = self._plot_rect()
        self._hover = self._idx_at(e.x(), r) if r.left() <= e.x() <= r.right() else None
        if self.mode == "points":
            if self._drag_node is not None and (e.buttons() & Qt.LeftButton):
                self._move_node(e.y(), r)
        elif e.buttons() & (Qt.LeftButton | Qt.RightButton):
            self._paint_at(e.x(), e.y(), r, bool(e.buttons() & Qt.RightButton))
        self.update()

    def mouseReleaseEvent(self, _e):
        self._drag_node = None
        self._last_idx = None
        self.update()

    def mouseDoubleClickEvent(self, e):
        if self.mode == "points":
            self._drag_node = self._nearest_node(e.x(), self._plot_rect())
            self.node_vals[self._drag_node] = 0.0
            self._rebuild()
            self._drag_node = None
            self.update()
            self.changed.emit()

    def leaveEvent(self, _e):
        self._hover = None
        self.update()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class SL88Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SL88 Editor – Studiologic SL88 Studio")
        self.setWindowIcon(make_app_icon())
        self.resize(1300, 900)
        self.setMinimumSize(1080, 720)

        self.worker = MidiWorker()
        self.worker.log.connect(self._append_log)
        self.worker.status.connect(self._set_status)
        self.worker.done.connect(self._on_job_done)
        self.worker.failed.connect(self._on_job_failed)
        self.worker.cancelled.connect(self._on_job_cancelled)
        self.worker.start()

        self.connected = False
        self.pending = 0
        self.dark = False
        self.names = {}
        self.curves = {}
        self.pdata = None
        self.current_program = None
        self._conn_widgets = []
        self._prog_widgets = []
        self.cards = []
        self.stick_panels = []
        self.pedal_panels = []
        self.curve_tiles = {}  # slot -> (plot, info, label, save_btn)
        self.groups = {}
        self.project = ex.new_project()
        self.project_path = None
        self.project_dirty = False
        self.cancel_flag = threading.Event()
        self.kb_memory = {}

        self._build_ui()
        self._build_menu()
        self._load_prefs()
        self.apply_theme(self.dark)
        self._set_connected(False)
        self.refresh_ports()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 4)
        root.setSpacing(6)

        bar = QFrame()
        bar.setObjectName("toolbar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(8, 6, 8, 6)
        bl.addWidget(QLabel("MIDI port:"))
        self.port_cb = QComboBox()
        self.port_cb.setEditable(True)
        self.port_cb.setMinimumWidth(280)
        self.port_cb.setEditText("SL STUDIO MIDI 2")
        bl.addWidget(self.port_cb)
        self.ports_btn = QPushButton("Refresh Ports")
        self.ports_btn.clicked.connect(self.refresh_ports)
        bl.addWidget(self.ports_btn)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_connect)
        bl.addWidget(self.connect_btn)
        self.dry_chk = QCheckBox("Dry Run (sends nothing)")
        self.dry_chk.toggled.connect(self._on_dry_toggle)
        bl.addWidget(self.dry_chk)
        self.adv_chk = QCheckBox("Allow unverified fields")
        self.adv_chk.setToolTip("Unlocks channel and key range in zone cards.")
        self.adv_chk.toggled.connect(lambda _c: self._update_buttons())
        bl.addWidget(self.adv_chk)
        bl.addStretch(1)
        self.conn_lbl = QLabel("● not connected")
        small_font(self.conn_lbl, 10)
        bl.addWidget(self.conn_lbl)
        root.addWidget(bar)

        strip = QFrame()
        strip.setObjectName("progstrip")
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(8, 6, 8, 6)
        self.prog_no_lbl = QLabel("P---")
        self.prog_no_lbl.setObjectName("led")
        f = QFont(self.prog_no_lbl.font())
        f.setPointSize(14)
        self.prog_no_lbl.setFont(f)
        sl.addWidget(self.prog_no_lbl)
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(p.NAME_LENGTH)
        self.name_edit.setMinimumWidth(220)
        f2 = QFont(self.name_edit.font())
        f2.setPointSize(13)
        f2.setBold(True)
        self.name_edit.setFont(f2)
        self.name_edit.returnPressed.connect(self.save_name)
        sl.addWidget(self.name_edit)
        self.save_btn = QPushButton("Save Name")
        self.save_btn.clicked.connect(self.save_name)
        sl.addWidget(self.save_btn)
        self.store_chk = QCheckBox("STORE (saves to memory)")
        self.store_chk.setChecked(True)
        self.verify_chk = QCheckBox("Verify / read back")
        sl.addWidget(self.store_chk)
        sl.addWidget(self.verify_chk)
        sl.addStretch(1)
        self.prog_info = QLabel("No program selected.")
        sl.addWidget(self.prog_info)
        root.addWidget(strip)
        self._prog_widgets += [self.name_edit, self.save_btn]

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_program_list())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_program_tab(), "PROGRAM")
        self.tabs.addTab(self._build_zone_tab(), "ZONE 1-4")
        self.tabs.addTab(self._build_sticks_tab(), "STICKS")
        self.tabs.addTab(self._build_pedals_tab(), "PEDALS")
        self.tabs.addTab(self._build_organize_tab(), "ORGANIZE")
        touch = QTabWidget()
        touch.addTab(self._build_curves_tab(), "SL CURVES")
        touch.addTab(self._build_keybalance_tab(), "KEY-BALANCE")
        self.tabs.addTab(touch, "SL TOUCH")
        self.tabs.addTab(self._build_log_tab(), "LOG")
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([250, 1000])
        root.addWidget(splitter, 1)

        self.keyboard = KeyboardWidget()
        root.addWidget(self.keyboard)



        self.status_lbl = QLabel("Ready. Choose MIDI port and click Connect.")
        self.statusBar().addWidget(self.status_lbl, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(150)
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.statusBar().addPermanentWidget(self.progress)

    def _build_menu(self):
        view = self.menuBar().addMenu("View")
        self.dark_action = QAction("Dark Mode", self)
        self.dark_action.setCheckable(True)
        self.dark_action.setShortcut("Ctrl+D")
        self.dark_action.toggled.connect(self._toggle_dark_mode)
        view.addAction(self.dark_action)

    def _build_program_list(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 4, 0)
        head = QLabel(f"PROGRAMS (1–{p.NUM_PROGRAMS})")
        small_font(head, 10)
        lay.addWidget(head)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter …")
        self.filter_edit.textChanged.connect(lambda _t: self._fill_tree())
        lay.addWidget(self.filter_edit)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["#", "Name"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnWidth(0, 50)
        self.tree.currentItemChanged.connect(self._on_select)
        lay.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.dump_btn = QPushButton("Read Config Dump")
        self.dump_btn.clicked.connect(self.load_dump)
        self.reload_btn = QPushButton("Reload Program")
        self.reload_btn.clicked.connect(self.reload_program)
        row.addWidget(self.dump_btn)
        row.addWidget(self.reload_btn)
        lay.addLayout(row)
        self._conn_widgets.append(self.dump_btn)
        self._prog_widgets.append(self.reload_btn)
        self._fill_tree()
        return w

    def _build_program_tab(self):
        w = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setSpacing(8)
        for z in range(1, p.NUM_ZONES + 1):
            card = ZoneCard(z)
            card.applyRequested.connect(self.apply_zone)
            self.cards.append(card)
            grid.addWidget(card, (z - 1) // 2, (z - 1) % 2)
        for c in (0, 1):
            grid.setColumnStretch(c, 1)
        for r in (0, 1):
            grid.setRowStretch(r, 1)
        note = QLabel("* channel and key range are raw values from program data; they are "
                      "locked until 'Allow unverified fields' is checked. "
                      "Change and click 'Apply Zone'.")
        small_font(note, 8, False)
        note.setWordWrap(True)
        grid.addWidget(note, 2, 0, 1, 2)
        return w

    def _build_log_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        head = QLabel("LOG – SysEx and status messages")
        small_font(head, 10)
        lay.addWidget(head)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(MAX_LOG_LINES)
        lay.addWidget(self.log, 1)
        clear = QPushButton("Clear Log")
        clear.clicked.connect(self.log.clear)
        lay.addWidget(clear, 0, Qt.AlignRight)
        return w

    def _build_zone_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        fields = ZONE_TABLE_ROWS
        self.zone_table = QTableWidget(len(fields), p.NUM_ZONES)
        self.zone_table.setVerticalHeaderLabels(fields)
        self.zone_table.setHorizontalHeaderLabels([f"ZONE {z}" for z in range(1, p.NUM_ZONES + 1)])
        self.zone_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.zone_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.zone_table.setSelectionMode(QAbstractItemView.NoSelection)
        lay.addWidget(self.zone_table)
        note = QLabel("All zone fields from program data (raw values). Key range is also shown as "
                      "note names and curve type as text. * = pedal values according to documentation "
                      "(offset 208–223), still unverified against hardware.")
        small_font(note, 8, False)
        lay.addWidget(note)
        self._fill_zone_table()
        return w

    def _build_sticks_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        top = QHBoxLayout()
        title = QLabel("STICK 1–3 · X/Y parameters per zone")
        small_font(title, 10)
        top.addWidget(title)
        top.addStretch(1)
        hint = QLabel("Off=0 · Pitch Bend=1 · Aftertouch=2 · CC N = N+2")
        small_font(hint, 8, False)
        top.addWidget(hint)
        self.sticks_all_btn = QPushButton("Apply All Zones")
        self.sticks_all_btn.clicked.connect(self.apply_all_sticks)
        top.addWidget(self.sticks_all_btn)
        self._prog_widgets.append(self.sticks_all_btn)
        lay.addLayout(top)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setSpacing(8)
        for z in range(1, p.NUM_ZONES + 1):
            panel = StickZonePanel(z)
            panel.applyStick.connect(self.apply_stick)
            panel.applyZone.connect(self.apply_zone_sticks)
            self.stick_panels.append(panel)
            il.addWidget(panel)
        note = QLabel("Values are ASSUMPTIONS based on pattern CC N = N+2 (Modulation = CC 1 = 3). "
                      "Verify against hardware with 'Verify / read back' checked.")
        note.setWordWrap(True)
        small_font(note, 8, False)
        il.addWidget(note)
        il.addStretch(1)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)
        return w

    def _build_pedals_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        top = QHBoxLayout()
        title = QLabel("PEDALS · Function per zone")
        small_font(title, 10)
        top.addWidget(title)
        top.addStretch(1)
        hint = QLabel("Off · Damper · Sostenuto · Soft Pedal · CC N = N+4")
        small_font(hint, 8, False)
        top.addWidget(hint)
        self.pedals_all_btn = QPushButton("Apply All Zones")
        self.pedals_all_btn.clicked.connect(self.apply_all_pedals)
        top.addWidget(self.pedals_all_btn)
        self._prog_widgets.append(self.pedals_all_btn)
        lay.addLayout(top)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setSpacing(8)
        for z in range(1, p.NUM_ZONES + 1):
            panel = PedalZonePanel(z)
            panel.applyPedal.connect(self.apply_pedal)
            panel.applyZone.connect(self.apply_zone_pedals)
            self.pedal_panels.append(panel)
            il.addWidget(panel)
        note = QLabel(
            "Values are ASSUMPTIONS based on pattern CC N = N+4 "
            "(Damper = 1, Soft Pedal = 3, CC 1 = 5, CC 74 = 78). "
            "Verify against hardware with 'Verify / read back' checked."
        )
        note.setWordWrap(True)
        small_font(note, 8, False)
        il.addWidget(note)
        il.addStretch(1)
        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)
        return w

    def _build_curves_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        head = QLabel("SL CURVES – curve definitions from config dump")
        small_font(head, 10)
        lay.addWidget(head)

        # Scroll area för att förhindra att rutorna krossas vertikalt
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setSpacing(14)
        grid.setContentsMargins(8, 8, 8, 8)

        order = [(1, "SOFT"), (0, "MEDIUM"), (2, "HARD")] + \
                [(3 + i, f"USER {i + 1}") for i in range(6)]

        for i, (slot, label) in enumerate(order):
            tile = QFrame()
            tile.setFrameShape(QFrame.StyledPanel)
            tile.setMinimumHeight(330)  # Garanterar tillräcklig höjd per ruta
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(8, 8, 8, 8)
            tl.setSpacing(6)

            title = QLabel(f"{i + 1} · {label}")
            small_font(title, 9)

            editable = (slot >= 3)  # Endast USER 1-6 är redigerbara
            plot = EditableCurvePlot()
            plot.setMinimumSize(140, 140)
            plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            plot.set_curve(None, {"SOFT": "soft", "HARD": "hard"}.get(label, "medium"), editable=editable)

            info = QLabel("–")
            small_font(info, 7, False)

            tl.addWidget(title)
            tl.addWidget(plot, 1)  # Kurvan får max stretch
            tl.addWidget(info)

            save_btn = None
            if editable:
                save_btn = QPushButton("Save to keyboard")
                save_btn.setEnabled(False)
                save_btn.setMinimumHeight(28)
                save_btn.clicked.connect(lambda checked, s=slot: self.save_curve_to_keyboard(s))
                tl.addWidget(save_btn)  # Hamnar garanterat sist under info
                plot.curveChanged.connect(lambda table, s=slot: self._on_curve_edited(s, table))

            self.curve_tiles[slot] = (plot, info, label, save_btn)
            grid.addWidget(tile, i // 3, i % 3)

        for c in range(3):
            grid.setColumnStretch(c, 1)

        scroll.setWidget(inner)
        lay.addWidget(scroll, 1)

        note = QLabel("medium/soft/hard = slots 0–2 (factory, read-only). "
                      "USER 1–6 = slots 3–8 (editable, can be saved to keyboard). "
                      "Drag the points to change the curve. Double-click = reset point.")
        small_font(note, 8, False)
        lay.addWidget(note)
        return w

    def _on_curve_edited(self, slot, table):
        if slot in self.curves:
            self.curves[slot]["table"] = table
        tile = self.curve_tiles.get(slot)
        if tile and tile[3]:
            tile[3].setEnabled(True)

    def save_curve_to_keyboard(self, slot):
        if not self.connected:
            QMessageBox.warning(self, "SL88", "Connect to the keyboard first.")
            return
        if slot < 3 or slot > 8:
            return
        tile = self.curve_tiles.get(slot)
        if not tile:
            return
        plot = tile[0]
        table = plot.get_table()
        if not table:
            QMessageBox.warning(self, "SL88", "No curve to save.")
            return
        reply = QMessageBox.question(
            self, "Save Curve",
            f"Save USER {slot - 2} to the keyboard? This will overwrite the existing curve.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        def fn(ed):
            require(ed)
            if hasattr(ed, 'send_curve'):
                ed.send_curve(slot, table)
                return {"ok": True}
            else:
                return {"ok": False, "error": "send_curve() is not implemented in sl88_editor.py"}

        def done(res):
            if res.get("ok"):
                self._set_status(f"USER {slot - 2} saved to keyboard.")
                tile[3].setEnabled(False)
            else:
                QMessageBox.critical(self, "SL88",
                    f"Could not save curve: {res.get('error', 'unknown error')}\n"
                    "The function needs to be implemented in sl88_editor.py.")

        self._submit(fn, done, f"Saving USER {slot - 2}", key="curve_save")

    def _build_organize_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        head = QLabel("ORGANIZE – programs and groups (keyboard ↔ local project)")
        small_font(head, 10)
        lay.addWidget(head)
        split = QSplitter(Qt.Horizontal)
        kb_w = QWidget()
        kb_lay = QVBoxLayout(kb_w)
        kb_lay.setContentsMargins(0, 0, 4, 0)
        kb_head = QLabel("KEYBOARD (from config dump)")
        small_font(kb_head, 9)
        kb_lay.addWidget(kb_head)
        self.org_kb_tree = QTreeWidget()
        self.org_kb_tree.setHeaderLabels(["#", "Name / Group"])
        self.org_kb_tree.setColumnWidth(0, 60)
        self.org_kb_tree.setAlternatingRowColors(True)
        kb_lay.addWidget(self.org_kb_tree, 1)
        split.addWidget(kb_w)
        pr_w = QWidget()
        pr_lay = QVBoxLayout(pr_w)
        pr_lay.setContentsMargins(4, 0, 0, 0)
        pr_head = QLabel("PROJECT (local)")
        small_font(pr_head, 9)
        pr_lay.addWidget(pr_head)
        self.org_pr_tree = QTreeWidget()
        self.org_pr_tree.setHeaderLabels(["#", "Name / Group"])
        self.org_pr_tree.setColumnWidth(0, 60)
        self.org_pr_tree.setAlternatingRowColors(True)
        pr_lay.addWidget(self.org_pr_tree, 1)
        split.addWidget(pr_w)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        lay.addWidget(split, 1)
        btn_row = QHBoxLayout()
        self.sync_in_btn = QPushButton("SYNC IN (fetch from keyboard)")
        self.sync_in_btn.clicked.connect(self.sync_in)
        btn_row.addWidget(self.sync_in_btn)
        self._prog_widgets.append(self.sync_in_btn)
        self.sync_out_btn = QPushButton("SYNC OUT (send to keyboard)")
        self.sync_out_btn.clicked.connect(self.sync_out)
        btn_row.addWidget(self.sync_out_btn)
        self._prog_widgets.append(self.sync_out_btn)
        btn_row.addSpacing(20)
        self.open_proj_btn = QPushButton("Open project …")
        self.open_proj_btn.clicked.connect(self.open_project)
        btn_row.addWidget(self.open_proj_btn)
        self.save_proj_btn = QPushButton("Save project …")
        self.save_proj_btn.clicked.connect(self.save_project)
        btn_row.addWidget(self.save_proj_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)
        note = QLabel(
            "SYNC IN fetches program names via config dump. Groups (0x03) are "
            "unverified against hardware – SYNC OUT for groups shows warning."
        )
        note.setWordWrap(True)
        small_font(note, 8, False)
        lay.addWidget(note)
        self._refresh_organize()
        return w

    def _build_keybalance_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        head = QLabel("KEY-BALANCE – sensitivity per key (±30 %)")
        small_font(head, 10)
        lay.addWidget(head)
        self.kb_editor = KeyBalanceEditor()
        self.kb_editor.changed.connect(self._kb_mark_dirty)
        lay.addWidget(self.kb_editor, 1)
        ctrl = QHBoxLayout()
        mode_lbl = QLabel("Mode:")
        small_font(mode_lbl, 9)
        ctrl.addWidget(mode_lbl)
        self.kb_mode_cb = QComboBox()
        self.kb_mode_cb.addItems(["Points (12 control points)", "Freehand (paint directly)"])
        self.kb_mode_cb.currentIndexChanged.connect(self._kb_on_mode_change)
        ctrl.addWidget(self.kb_mode_cb)
        ctrl.addSpacing(20)
        mem_lbl = QLabel("Memory:")
        small_font(mem_lbl, 9)
        ctrl.addWidget(mem_lbl)
        self.kb_mem_cb = QComboBox()
        self.kb_mem_cb.addItems([f"Memory {i}" for i in range(1, 11)])
        ctrl.addWidget(self.kb_mem_cb)
        ctrl.addSpacing(20)
        self.kb_load_mem_btn = QPushButton("Load memory")
        self.kb_load_mem_btn.clicked.connect(self.kb_load_memory)
        ctrl.addWidget(self.kb_load_mem_btn)
        self.kb_save_mem_btn = QPushButton("Save memory")
        self.kb_save_mem_btn.clicked.connect(self.kb_save_memory)
        ctrl.addWidget(self.kb_save_mem_btn)
        ctrl.addStretch(1)
        lay.addLayout(ctrl)
        kb_row = QHBoxLayout()
        self.kb_read_btn = QPushButton("Read from keyboard")
        self.kb_read_btn.clicked.connect(self.kb_load_from_keyboard)
        kb_row.addWidget(self.kb_read_btn)
        self._prog_widgets.append(self.kb_read_btn)
        self.kb_write_btn = QPushButton("Save to keyboard")
        self.kb_write_btn.clicked.connect(self.kb_save_to_keyboard)
        kb_row.addWidget(self.kb_write_btn)
        self._prog_widgets.append(self.kb_write_btn)
        kb_row.addStretch(1)
        lay.addLayout(kb_row)
        note = QLabel(
            "Key-balance is UNVERIFIED against hardware. Values are percentages (±30 %). "
            "Double-click resets a point in points mode. Right-click = 0 % in freehand."
        )
        note.setWordWrap(True)
        small_font(note, 8, False)
        lay.addWidget(note)
        return w

    def _prefs_path(self):
        return Path.home() / ".sl88_gui_prefs.json"

    def _read_prefs(self):
        try:
            return json.loads(self._prefs_path().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _load_prefs(self):
        self.dark = bool(self._read_prefs().get("dark_mode", False))
        self.dark_action.blockSignals(True)
        self.dark_action.setChecked(self.dark)
        self.dark_action.blockSignals(False)

    def _save_prefs(self):
        data = self._read_prefs()
        data["dark_mode"] = self.dark
        try:
            self._prefs_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _toggle_dark_mode(self, checked):
        self.apply_theme(bool(checked))
        self._save_prefs()

    def apply_theme(self, dark):
        self.dark = dark
        QApplication.instance().setStyleSheet(build_qss(dark))
        for card in self.cards:
            card.apply_theme(dark)
        for panel in self.stick_panels:
            panel.apply_theme(dark)
        self.keyboard.set_dark(dark)
        self._fill_zone_table()
        self._update_conn_label()

    def _update_conn_label(self):
        self.conn_lbl.setText("● connected" if self.connected else "● not connected")
        self.conn_lbl.setStyleSheet(
            "color: %s;" % ("#2fb344" if self.connected else "#d64545"))

    def _submit(self, fn, on_done=None, label="Working", key=None):
        self.pending += 1
        if self.pending == 1:
            self.progress.setRange(0, 0)
        self._update_buttons()
        self.worker.submit(Job(fn, on_done, label, key))

    def _job_finished(self):
        self.pending = max(0, self.pending - 1)
        if self.pending == 0:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
        self._update_buttons()

    def _set_status(self, text):
        self.status_lbl.setText(text)

    def _on_job_done(self, job, result):
        self._job_finished()
        self._set_status(f"Done – {job.label}")
        if job.on_done:
            try:
                job.on_done(result)
            except Exception:
                self._append_log(traceback.format_exc().rstrip())

    def _on_job_failed(self, job, text):
        self._job_finished()
        self._append_log("ERROR: " + text)
        self._set_status("Error: " + text)
        if job.key == "connect":
            self._set_connected(False)
            QMessageBox.critical(self, "Connection failed", text)

    def _on_job_cancelled(self, _job):
        self._job_finished()

    def _append_log(self, text):
        self.log.appendPlainText(text.rstrip("\n"))

    def _set_connected(self, flag):
        self.connected = flag
        self.connect_btn.setText("Disconnect" if flag else "Connect")
        self.port_cb.setEnabled(not flag)
        self._update_conn_label()
        self._update_buttons()

    def _update_buttons(self):
        idle = self.pending == 0
        live = self.connected and idle
        has_prog = self.current_program is not None
        editable = live and has_prog
        for w in self._conn_widgets:
            w.setEnabled(live)
        for w in self._prog_widgets:
            w.setEnabled(editable)
        adv = self.adv_chk.isChecked()
        for card in self.cards:
            card.set_editable(editable, adv)
        for panel in self.stick_panels:
            panel.set_editable(editable)
        self.connect_btn.setEnabled(idle)
        self.ports_btn.setEnabled(idle)

    def _fill_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        filt = self.filter_edit.text().strip().lower()
        sel_item = None
        for num in range(1, p.NUM_PROGRAMS + 1):
            name = self.names.get(num - 1, "")
            if filt and filt not in str(num) and filt not in name.lower():
                continue
            it = QTreeWidgetItem([str(num), name or "–"])
            it.setData(0, Qt.UserRole, num)
            it.setTextAlignment(0, Qt.AlignRight | Qt.AlignVCenter)
            self.tree.addTopLevelItem(it)
            if num == self.current_program:
                sel_item = it
        if sel_item is not None:
            self.tree.setCurrentItem(sel_item)
            self.tree.scrollToItem(sel_item)
        self.tree.blockSignals(False)

    def _fill_curves(self):
        for slot, tile_data in self.curve_tiles.items():
            plot = tile_data[0]
            info = tile_data[1]
            label = tile_data[2]
            save_btn = tile_data[3] if len(tile_data) > 3 else None
            
            c = self.curves.get(slot)
            choice = {1: "soft", 0: "medium", 2: "hard"}.get(slot)
            editable = (slot >= 3)
            if c:
                plot.set_curve(c["table"], choice, editable=editable)
                kind = "user" if c["type"] == 0x55 else "factory"
                info.setText(f"{c['name']} · {kind} · checksum "
                             f"{'OK' if c['checksum_ok'] else 'FAIL'}")
            else:
                plot.set_curve(None, choice, editable=editable)
                info.setText("no data")
            
            if save_btn:
                save_btn.setEnabled(False)

    def _fill_zone_table(self):
        zones = self.pdata["zones"] if self.pdata else None
        for r, field in enumerate(ZONE_TABLE_ROWS):
            for c in range(p.NUM_ZONES):
                if zones is None:
                    text = "–"
                elif field.startswith("pedal_"):
                    v = self.pdata["array"][ex.pedal_offset(int(field[6:]), c + 1)]
                    text = f"{v}  ({ex.describe_pedal_value(v)})  *"
                else:
                    v = zones[c][field]
                    if field in ("low_key", "high_key"):
                        text = f"{v}  ({note_name(v)})"
                    elif field == "curve_type":
                        text = f"0x{v:02X}  ({p.describe_curve_type(v)})"
                    else:
                        text = str(v)
                item = QTableWidgetItem(text)
                st = ZONE_STYLES[c + 1]
                if self.dark:
                    item.setBackground(QBrush(QColor(mix("#23272d", st["bg"], 0.30))))
                    item.setForeground(QBrush(QColor("#eceff1")))
                else:
                    item.setBackground(QBrush(QColor(st["tint"])))
                    item.setForeground(QBrush(QColor(st["ink"])))
                self.zone_table.setItem(r, c, item)

    def _clear_zones(self):
        self.pdata = None
        for card in self.cards:
            card.clear()
        for panel in self.stick_panels:
            panel.clear()
        self._fill_zone_table()
        self.keyboard.set_zones(None)

    def _show_program(self, num, pdata):
        self.current_program = num
        self.pdata = pdata
        self.prog_no_lbl.setText(f"P{num:03d}")
        self.name_edit.setText(pdata["name"])
        chk = "OK" if pdata["checksum_ok"] else "FAIL"
        self.prog_info.setText(f"Program {num} • name {pdata['name']!r} • checksum {chk}")
        if not pdata["checksum_ok"]:
            self._append_log(f"WARNING: checksum error in program data for program {num}.")
        for z, zd in enumerate(pdata["zones"]):
            self.cards[z].load(zd, self.curves)
            self.stick_panels[z].load(zd, self._append_log)
            self.pedal_panels[z].load(pdata['array'], self._append_log)
        self._fill_zone_table()
        self.keyboard.set_zones([(zd["low_key"], zd["high_key"], zd["enabled"])
                                 for zd in pdata["zones"]])
        self._update_buttons()

    def refresh_ports(self):
        current = self.port_cb.currentText().strip()

        def fn(_ed):
            return {"in": mido.get_input_names(), "out": mido.get_output_names()}

        def done(res):
            values = sorted(set(res["in"]) | set(res["out"]))
            self.port_cb.clear()
            self.port_cb.addItems(values)
            if values and not any(current.lower() in v.lower() for v in values):
                sl = [v for v in values if "sl" in v.lower() and "studio" in v.lower()]
                pref = [v for v in sl if "midi 2" in v.lower()] or sl
                current_new = pref[0] if pref else values[0]
            else:
                current_new = current
            self.port_cb.setEditText(current_new)
            self._append_log(f"Inputs: {res['in']}")
            self._append_log(f"Outputs: {res['out']}")
            self._set_status(f"Found {len(values)} MIDI ports.")

        self._submit(fn, done, "Listing MIDI ports", key="ports")

    def toggle_connect(self):
        if self.connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        port = self.port_cb.currentText().strip()
        if not port:
            QMessageBox.warning(self, "SL88", "Enter a MIDI port (part of the name is enough).")
            return
        dry = self.dry_chk.isChecked()

        def fn(_ed):
            self.worker.close_editor()
            self.worker.editor = SL88Editor(port, dry_run=dry)
            return {"port": port}

        def done(res):
            self._set_connected(True)
            self._set_status(f"Connected to {res['port']}.")
            self.load_dump()

        self._submit(fn, done, f"Connecting to '{port}'", key="connect")

    def _disconnect(self):
        def fn(_ed):
            self.worker.close_editor()
            return {}

        def done(_res):
            self._set_connected(False)
            self._set_status("Disconnected.")

        self._submit(fn, done, "Disconnecting", key="connect")

    def _on_dry_toggle(self, dry):
        if not self.connected:
            return

        def fn(ed):
            require(ed).dry_run = dry
            return {"dry": dry}

        def done(res):
            self._set_status("Dry Run ON – nothing sent." if res["dry"]
                             else "Dry Run OFF – sending for real.")

        self._submit(fn, done, "Updating dry run", key="dry")

    def load_dump(self):
        def fn(ed):
            require(ed)
            count = ed.request_config_dump(timeout=DUMP_TIMEOUT, summary=False)
            return {"count": count, **snapshot(ed)}

        def done(res):
            self.names = res["names"]
            self.curves = res["curves"]
            self._fill_tree()
            self._fill_curves()
            self._set_status(f"Dump: {res['count']} messages, "
                             f"{len(self.names)} program names, {len(self.curves)} curves.")

        self._submit(fn, done, "Reading config dump", key="dump")

    def _on_select(self, cur, _prev):
        if cur is None:
            return
        num = cur.data(0, Qt.UserRole)
        if num is None or num == self.current_program:
            return
        if not self.connected:
            self._set_status("Not connected – click Connect first.")
            return
        if self.dry_chk.isChecked():
            self.current_program = num
            self._clear_zones()
            self.prog_no_lbl.setText(f"P{num:03d}")
            self.name_edit.setText(self.names.get(num - 1, ""))
            self.prog_info.setText(f"Program {num} (dry run – no reading)")
            self._set_status(f"Dry run: program {num} selected, nothing read.")
            self._update_buttons()
            return
        self.load_program(num)

    def load_program(self, num):
        self.current_program = num
        self._clear_zones()
        self.prog_no_lbl.setText(f"P{num:03d}")
        self.prog_info.setText(f"Program {num} – reading …")
        self._update_buttons()

        def fn(ed):
            require(ed)
            pdata = ed.read_program(num)
            return {"pdata": pdata, **snapshot(ed)}

        def done(res):
            if res["names"]:
                self.names = res["names"]
                self.curves = res["curves"]
                self._fill_tree()
                self._fill_curves()
            pdata = res["pdata"]
            if pdata is None:
                self.prog_info.setText(f"Program {num} – no program data received.")
                self._set_status(f"Program {num}: no program data (01) in response.")
                return
            self._show_program(num, pdata)
            self._set_status(f"Program {num} loaded: {pdata['name']!r}")

        self._submit(fn, done, f"Reading program {num}", key="load")

    def reload_program(self):
        if self.current_program is not None:
            self.load_program(self.current_program)

    def save_name(self):
        num = self.current_program
        if num is None:
            QMessageBox.information(self, "SL88", "Select a program from the list first.")
            return
        name = self.name_edit.text()
        store, verify = self.store_chk.isChecked(), self.verify_chk.isChecked()

        def fn(ed):
            require(ed)
            ok = ed.rename_program(num, name, store=store, verify=verify)
            return {"ok": ok, **snapshot(ed)}

        def done(res):
            if res["dry_run"]:
                self._set_status("Dry run – name was not sent.")
                return
            self.names = res["names"] or self.names
            if not store:
                self.names[num - 1] = name.rstrip()
            self._fill_tree()
            if res["ok"] is False:
                self._set_status(f"Verification DEVIATED for program {num} – see log.")
            elif store:
                self._set_status(f"Program {num} saved as {name.rstrip()!r}.")
            else:
                self._set_status(f"Name changed in RAM for program {num} "
                                 "(STORE off – disappears on program change).")

        self._submit(fn, done, f"Renaming program {num}", key="write")

    def apply_zone(self, zone, changes):
        num = self.current_program
        if num is None:
            QMessageBox.information(self, "SL88", "Select a program from the list first.")
            return
        if not changes:
            self._set_status(f"No changes to send in zone {zone}.")
            return
        zd = self.pdata["zones"][zone - 1] if self.pdata else {}
        lo = changes.get("low_key", zd.get("low_key"))
        hi = changes.get("high_key", zd.get("high_key"))
        if lo is not None and hi is not None and lo > hi:
            QMessageBox.warning(self, "SL88",
                                f"Lowest key ({note_name(lo)}) is higher than highest ({note_name(hi)}).")
            return
        store, verify = self.store_chk.isChecked(), self.verify_chk.isChecked()
        label = f"Setting zone {zone}: " + ", ".join(sorted(changes))

        def fn(ed):
            require(ed)
            ed.recall_program(num)
            for field in ("volume", "midi_channel", "low_key", "high_key", "midi_port", "enabled"):
                if field in changes:
                    val = int(changes[field])
                    if field == "midi_channel":
                        val -= 1
                    ed.set_zone_field(field, zone, val)
            if "curve" in changes:
                ed.set_velocity_curve(changes["curve"], zone=zone)
            if store:
                ed.store_program(num)
            pdata = ed.read_program(num) if (verify and not ed.dry_run) else None
            return {"pdata": pdata, **snapshot(ed)}

        def done(res):
            if res["dry_run"]:
                self._set_status(f"Dry run – {label} was not sent.")
                return
            if res["pdata"] is not None:
                self.names = res["names"] or self.names
                self._fill_tree()
                self._show_program(num, res["pdata"])
                self._set_status(f"{label}: sent and read back.")
                return
            if self.pdata:
                z = self.pdata["zones"][zone - 1]
                for f, v in changes.items():
                    if f == "curve":
                        z["curve_type"] = p.curve_type_value(v)
                    elif f == "midi_channel":
                        z[f] = v - 1
                    else:
                        z[f] = v
                self._show_program(num, self.pdata)
            self._set_status(f"{label}: sent"
                             + (" and saved (STORE)." if store else " (not STORE)."))

        self._submit(fn, done, label, key="write")

    def _apply_sticks(self, changes, label):
        num = self.current_program
        store, verify = self.store_chk.isChecked(), self.verify_chk.isChecked()

        def fn(ed):
            require(ed)
            if not ed.dry_run:
                ed.recall_program(num)
            for zone, items in changes.items():
                for s, xv, yv in items:
                    ed.set_zone_field(p.stick_field(s, "x"), zone, xv)
                    ed.set_zone_field(p.stick_field(s, "y"), zone, yv)
            if store:
                ed.store_program(num)
            pdata = ed.read_program(num) if (verify and not ed.dry_run) else None
            return {"pdata": pdata, **snapshot(ed)}

        def done(res):
            if res["dry_run"]:
                self._set_status(f"Dry run – {label} was not sent.")
                return
            if res["pdata"] is not None:
                self.names = res["names"] or self.names
                self._fill_tree()
                self._show_program(num, res["pdata"])
                self._set_status(f"{label}: sent and read back – see STICKS.")
                return
            if self.pdata:
                for zone, items in changes.items():
                    for s, xv, yv in items:
                        self.pdata["zones"][zone - 1][p.stick_field(s, "x")] = xv
                        self.pdata["zones"][zone - 1][p.stick_field(s, "y")] = yv
                self._show_program(num, self.pdata)
            self._set_status(f"{label}: sent"
                             + (" and saved (STORE)." if store else " (not STORE)."))

        self._submit(fn, done, label, key="write")

    def apply_stick(self, zone, stick):
        if self.current_program is None:
            QMessageBox.information(self, "SL88", "Select a program from the list first.")
            return
        items = self.stick_panels[zone - 1].collect([stick])
        if items is not None:
            self._apply_sticks({zone: items}, f"Setting STICK {stick} X/Y in zone {zone}")

    def apply_zone_sticks(self, zone):
        if self.current_program is None:
            QMessageBox.information(self, "SL88", "Select a program from the list first.")
            return
        items = self.stick_panels[zone - 1].collect(range(1, p.NUM_STICKS + 1))
        if items is not None:
            self._apply_sticks({zone: items}, f"Setting all sticks in zone {zone}")

    def apply_all_sticks(self):
        if self.current_program is None:
            QMessageBox.information(self, "SL88", "Select a program from the list first.")
            return
        changes = {}
        for z in range(1, p.NUM_ZONES + 1):
            items = self.stick_panels[z - 1].collect(range(1, p.NUM_STICKS + 1))
            if items is None:
                return
            changes[z] = items
        self._apply_sticks(changes, "Setting all sticks in all zones (24 fields)")

    def _apply_pedals(self, changes, label):
        num = self.current_program
        store, verify = self.store_chk.isChecked(), self.verify_chk.isChecked()

        def fn(ed):
            require(ed)
            if not ed.dry_run:
                ed.recall_program(num)
            for zone, items in changes.items():
                for n, value in items:
                    msg = p.build_alter_active_program(
                        ex.pedal_offset(n, zone), [int(value) * 128])
                    ed.send_raw(msg)
            if store:
                ed.store_program(num)
            if verify and not ed.dry_run:
                pdata = ed.read_program(num)
            else:
                pdata = None
            return {"pdata": pdata, **snapshot(ed)}

        def done(res):
            if res["dry_run"]:
                self._set_status(f"Dry run – {label} was not sent.")
                return
            if res["pdata"] is not None:
                self.names = res["names"] or self.names
                self._fill_tree()
                self._show_program(num, res["pdata"])
                self._set_status(f"{label}: sent and read back – see PEDALS.")
                return
            if self.pdata:
                for zone, items in changes.items():
                    for n, value in items:
                        self.pdata["array"][ex.pedal_offset(n, zone)] = value
                self._show_program(num, self.pdata)
            self._set_status(f"{label}: sent"
                             + (" and saved (STORE)." if store else " (not STORE)."))

        self._submit(fn, done, label, key="write")

    def apply_pedal(self, zone, pedal):
        if self.current_program is None:
            QMessageBox.information(self, "SL88", "Select a program from the list first.")
            return
        items = self.pedal_panels[zone - 1].collect([pedal])
        if items is not None:
            self._apply_pedals({zone: items}, f"Setting PEDAL {pedal} in zone {zone}")

    def apply_zone_pedals(self, zone):
        if self.current_program is None:
            QMessageBox.information(self, "SL88", "Select a program from the list first.")
            return
        items = self.pedal_panels[zone - 1].collect(range(1, ex.NUM_PEDALS + 1))
        if items is not None:
            self._apply_pedals({zone: items}, f"Setting all pedals in zone {zone}")

    def apply_all_pedals(self):
        if self.current_program is None:
            QMessageBox.information(self, "SL88", "Select a program from the list first.")
            return
        changes = {}
        for z in range(1, p.NUM_ZONES + 1):
            items = self.pedal_panels[z - 1].collect(range(1, ex.NUM_PEDALS + 1))
            if items is None:
                return
            changes[z] = items
        self._apply_pedals(changes, "Setting all pedals in all zones (16 fields)")

    def _refresh_organize(self):
        if not hasattr(self, "org_kb_tree"):
            return
        self.org_kb_tree.clear()
        prog_root = QTreeWidgetItem(["", "PROGRAMS (1–250)"])
        prog_root.setExpanded(False)
        self.org_kb_tree.addTopLevelItem(prog_root)
        for num in range(1, p.NUM_PROGRAMS + 1):
            name = self.names.get(num - 1, "")
            it = QTreeWidgetItem([str(num), name or "–"])
            prog_root.addChild(it)
        grp_root = QTreeWidgetItem(["", "GROUPS (0–7)"])
        grp_root.setExpanded(False)
        self.org_kb_tree.addTopLevelItem(grp_root)
        for g in range(8):
            gd = self.groups.get(g)
            if gd:
                name = gd.get("name", "") or "(empty)"
                count = len(gd.get("programs", []))
                it = QTreeWidgetItem([str(g), f"{name}  ({count} programs)"])
            else:
                it = QTreeWidgetItem([str(g), "(not read)"])
            grp_root.addChild(it)
        self.org_pr_tree.clear()
        proj_prog = QTreeWidgetItem(
            ["", f"PROGRAMS ({len(self.project.get('programs', []))})"]
        )
        proj_prog.setExpanded(False)
        self.org_pr_tree.addTopLevelItem(proj_prog)
        for i, num in enumerate(self.project.get("programs", []), 1):
            name = self.names.get(num - 1, "")
            proj_prog.addChild(
                QTreeWidgetItem([str(i), f"P{num:03d}  {name or '–'}"])
            )
        proj_grp = QTreeWidgetItem(["", "GROUPS (0–7)"])
        proj_grp.setExpanded(False)
        self.org_pr_tree.addTopLevelItem(proj_grp)
        for g in range(8):
            gd = self.project.get("groups", {}).get(g, {})
            name = gd.get("name", "") or "(empty)"
            count = len(gd.get("programs", []))
            proj_grp.addChild(
                QTreeWidgetItem([str(g), f"{name}  ({count} programs)"])
            )

    def sync_in(self):
        if not self.connected:
            QMessageBox.warning(self, "SL88", "Connect to the keyboard first.")
            return

        def fn(ed):
            require(ed)
            count = ed.request_config_dump(timeout=DUMP_TIMEOUT, summary=False)
            groups_ok = 0
            for g in range(8):
                try:
                    ed.send_raw([ex.GROUP_MSG, g])
                    time.sleep(0.15)
                    groups_ok += 1
                except Exception as exc:
                    self._append_log(f"  (group {g}: {exc})")
            return {"count": count, "groups_ok": groups_ok, **snapshot(ed)}

        def done(res):
            self.names = res["names"] or self.names
            self.groups = res.get("groups", self.groups)
            self.project["programs"] = list(range(1, p.NUM_PROGRAMS + 1))
            for g in range(8):
                if g in self.groups:
                    self.project["groups"][g] = dict(self.groups[g])
            self._fill_tree()
            self._refresh_organize()
            self._set_status(
                f"SYNC IN: {res['count']} messages, "
                f"{len(self.names)} programs, {len(self.groups)} groups."
            )

        self._submit(fn, done, "SYNC IN – fetching from keyboard", key="dump")

    def sync_out(self):
        if not self.connected:
            QMessageBox.warning(self, "SL88", "Connect to the keyboard first.")
            return
        reply = QMessageBox.warning(
            self, "SYNC OUT",
            "Group messages (0x03) are NOT verified against hardware.\n"
            "Send project groups anyway? (Programs are not sent in bulk.)",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        def fn(ed):
            require(ed)
            sent = 0
            for g in range(8):
                gd = self.project.get("groups", {}).get(g)
                if not gd:
                    continue
                try:
                    msg = ex.build_group(g, gd.get("name", ""), gd.get("programs", []))
                    ed.send_raw(msg)
                    time.sleep(0.15)
                    sent += 1
                except Exception as exc:
                    self._append_log(f"  (group {g}: {exc})")
            return {"sent": sent}

        def done(res):
            self._set_status(f"SYNC OUT: sent {res['sent']} groups (unverified).")

        self._submit(fn, done, "SYNC OUT – sending groups", key="write")

    def save_project(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save project",
            self.project_path or str(Path.home() / "sl88_project.json"),
            "JSON (*.json)"
        )
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self.project, indent=2), encoding="utf-8")
            self.project_path = path
            self.project_dirty = False
            self._set_status(f"Project saved: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "SL88", f"Could not save: {exc}")

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", str(Path.home()), "JSON (*.json)"
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.project = data
            self.project_path = path
            self.project_dirty = False
            self._refresh_organize()
            self._set_status(f"Project opened: {path}")
        except Exception as exc:
            QMessageBox.critical(self, "SL88", f"Could not open: {exc}")

    def _kb_mark_dirty(self):
        self.project_dirty = True

    def _kb_on_mode_change(self, idx):
        mode = "points" if idx == 0 else "freehand"
        self.kb_editor.set_mode(mode)

    def kb_load_from_keyboard(self):
        if not self.connected:
            QMessageBox.warning(self, "SL88", "Connect to the keyboard first.")
            return
        QMessageBox.information(
            self, "Key-balance",
            "Key-balance is UNVERIFIED against hardware.\n"
            "This function is not implemented yet – wait for protocol test."
        )

    def kb_save_to_keyboard(self):
        if not self.connected:
            QMessageBox.warning(self, "SL88", "Connect to the keyboard first.")
            return
        QMessageBox.information(
            self, "Key-balance",
            "Key-balance is UNVERIFIED against hardware.\n"
            "This function is not implemented yet – wait for protocol test."
        )

    def kb_load_memory(self):
        slot = self.kb_mem_cb.currentIndex() + 1
        vals = self.kb_memory.get(slot)
        if vals is None:
            QMessageBox.information(self, "Key-balance", f"Memory {slot} is empty.")
            return
        self.kb_editor.set_values(vals)
        self._set_status(f"Memory {slot} loaded to editor.")

    def kb_save_memory(self):
        slot = self.kb_mem_cb.currentIndex() + 1
        self.kb_memory[slot] = self.kb_editor.get_values()
        self._set_status(f"Memory {slot} saved locally.")

    def closeEvent(self, event):
        self.worker.stop()
        if not self.worker.wait(3000):
            self.worker.terminate()
            self.worker.wait()
        event.accept()


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = SL88Window()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
