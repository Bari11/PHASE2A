"""
warning_banner.py — Canary Pre-Unplug Warning Banner
=====================================================
A slim, non-intrusive banner pinned to the top of the screen.
  • Shows when the same bad signal fires 3x in a row
  • 2-minute countdown with pulsing glow
  • One-time +5 min snooze button
  • Auto-fires Unplug Mode on countdown expiry

Usage (from tray_app.py / detector callback)
---------------------------------------------
    banner = WarningBanner()
    banner.unplug_requested.connect(trigger_unplug_mode)
    banner.show()
"""

import math
import sys

from PyQt5.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QApplication
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QRectF, QPropertyAnimation, QEasingCurve
from PyQt5.QtGui import (
    QPainter, QColor, QLinearGradient, QFont,
    QPen, QBrush, QPainterPath
)


# ── Constants ─────────────────────────────────────────────────────────────────

COUNTDOWN_SECONDS = 120   # 2 minutes
SNOOZE_SECONDS    = 300   # +5 minutes


class WarningBanner(QWidget):
    """
    Thin full-width banner that sits at the very top of the primary screen.

    Signals
    -------
    unplug_requested  — emitted when countdown expires OR user clicks "Take Break"
    snoozed           — emitted when user uses their one snooze
    dismissed         — emitted if user somehow closes without snooze/break
    """

    unplug_requested = pyqtSignal()
    snoozed          = pyqtSignal(int)   # emits snooze duration in seconds
    dismissed        = pyqtSignal()

    def __init__(self, signal_name: str = 'general', parent=None):
        super().__init__(parent)
        self._signal_name  = signal_name
        self._seconds_left = COUNTDOWN_SECONDS
        self._snoozed      = False
        self._glow_t       = 0.0
        self._urgency      = 0.0   # 0→1 as countdown approaches zero

        self._setup_window()
        self._build_ui()
        self._start_timers()

    # ── Window setup ──────────────────────────────────────────

    def _setup_window(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool |
            Qt.X11BypassWindowManagerHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        screen = QApplication.primaryScreen().geometry()
        banner_h = 52
        self.setGeometry(screen.x(), screen.y(), screen.width(), banner_h)

    # ── UI ────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 0, 16, 0)
        layout.setSpacing(14)

        # Bird icon
        bird = QLabel('🐦')
        bird.setStyleSheet('font-size: 18px; background: transparent;')
        layout.addWidget(bird)

        # Message
        msg_text = self._signal_message()
        self._msg_label = QLabel(msg_text)
        self._msg_label.setStyleSheet("""
            color: rgba(255, 248, 230, 0.95);
            font-size: 13px;
            font-family: 'Georgia', serif;
            background: transparent;
            letter-spacing: 0.3px;
        """)
        layout.addWidget(self._msg_label)

        layout.addStretch()

        # Countdown label
        self._timer_label = QLabel('2:00')
        self._timer_label.setAlignment(Qt.AlignCenter)
        self._timer_label.setFixedWidth(52)
        self._timer_label.setStyleSheet("""
            color: rgba(255, 240, 180, 0.95);
            font-size: 15px;
            font-weight: bold;
            font-family: 'Georgia', serif;
            background: transparent;
            letter-spacing: 1px;
        """)
        layout.addWidget(self._timer_label)

        # Snooze button (one use)
        self._snooze_btn = QPushButton('+5 min')
        self._snooze_btn.setFixedSize(74, 30)
        self._snooze_btn.setCursor(Qt.PointingHandCursor)
        self._snooze_btn.setStyleSheet(self._snooze_style(enabled=True))
        self._snooze_btn.clicked.connect(self._on_snooze)
        layout.addWidget(self._snooze_btn)

        # Take Break button
        self._break_btn = QPushButton('Take Break')
        self._break_btn.setFixedSize(96, 30)
        self._break_btn.setCursor(Qt.PointingHandCursor)
        self._break_btn.setStyleSheet(self._break_style())
        self._break_btn.clicked.connect(self._on_take_break)
        layout.addWidget(self._break_btn)

    # ── Timers ────────────────────────────────────────────────

    def _start_timers(self):
        # 1-second countdown tick
        self._countdown_timer = QTimer(self)
        self._countdown_timer.timeout.connect(self._tick_countdown)
        self._countdown_timer.start(1000)

        # Animation repaint at ~30 fps
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick_anim)
        self._anim_timer.start(33)

    # ── Countdown logic ───────────────────────────────────────

    def _tick_countdown(self):
        self._seconds_left -= 1
        self._urgency = max(0.0, 1.0 - self._seconds_left / COUNTDOWN_SECONDS)

        m = self._seconds_left // 60
        s = self._seconds_left  % 60
        self._timer_label.setText(f'{m}:{s:02d}')

        # Colour timer label red as urgency rises
        red_a = int(200 + 55 * self._urgency)
        grn_a = int(240 - 80 * self._urgency)
        self._timer_label.setStyleSheet(f"""
            color: rgba({red_a}, {grn_a}, 150, 0.95);
            font-size: {int(15 + 3 * self._urgency)}px;
            font-weight: bold;
            font-family: 'Georgia', serif;
            background: transparent;
            letter-spacing: 1px;
        """)

        if self._seconds_left <= 0:
            self._fire_unplug()

    def _tick_anim(self):
        self._glow_t += 0.05
        self.update()

    # ── Paint — background with animated glow ─────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()

        # Glow pulse speed increases with urgency
        pulse_speed = 1.0 + self._urgency * 3.5
        glow = 0.5 + 0.5 * math.sin(self._glow_t * pulse_speed)

        # Base gradient — deep forest-purple, shifts to amber/red with urgency
        r0 = int(28  + 60  * self._urgency)
        g0 = int(22  - 10  * self._urgency)
        b0 = int(42  - 20  * self._urgency)

        r1 = int(38  + 80  * self._urgency)
        g1 = int(30  - 15  * self._urgency)
        b1 = int(55  - 25  * self._urgency)

        bg = QLinearGradient(0, 0, w, 0)
        bg.setColorAt(0.0, QColor(r0, g0, b0, 230))
        bg.setColorAt(0.5, QColor(r1, g1, b1, 240))
        bg.setColorAt(1.0, QColor(r0, g0, b0, 230))

        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), 0, 0)
        p.fillPath(path, QBrush(bg))

        # Bottom glow line — pulses
        alpha = int(80 + 120 * glow + 80 * self._urgency)
        gr  = int(180 + 60 * self._urgency)
        gg  = int(120 - 80 * self._urgency)
        gb  = int(80  - 40 * self._urgency)
        pen = QPen(QColor(gr, gg, gb, alpha), 1.5)
        p.setPen(pen)
        p.drawLine(0, h - 1, w, h - 1)

        # Subtle shimmer overlay
        shimmer_x = (self._glow_t * 80) % (w + 200) - 100
        shimmer = QLinearGradient(shimmer_x, 0, shimmer_x + 180, 0)
        shimmer.setColorAt(0.0, QColor(255, 255, 255, 0))
        shimmer.setColorAt(0.5, QColor(255, 255, 255, int(12 + 8 * glow)))
        shimmer.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(0, 0, w, h, QBrush(shimmer))

    # ── Button actions ────────────────────────────────────────

    def _on_snooze(self):
        if self._snoozed:
            return
        self._snoozed = True
        self._seconds_left += SNOOZE_SECONDS
        self._snooze_btn.setEnabled(False)
        self._snooze_btn.setText('Snoozed')
        self._snooze_btn.setStyleSheet(self._snooze_style(enabled=False))
        self.snoozed.emit(SNOOZE_SECONDS)

    def _on_take_break(self):
        self._fire_unplug()

    def _fire_unplug(self):
        self._countdown_timer.stop()
        self._anim_timer.stop()
        self.unplug_requested.emit()
        self.close()

    # ── Helpers ───────────────────────────────────────────────

    def _signal_message(self) -> str:
        messages = {
            'brow_contraction':  'Brow tension detected — time to rest your eyes.',
            'forward_head':      'Neck strain building — posture check incoming.',
            'rounded_shoulders': 'Shoulders creeping forward — a stretch break is due.',
            'jaw_clench':        'Jaw tension detected — loosen up.',
            'slouch':            'You\'ve been slouching — time to reset.',
            'eye_narrowing':     'Eye strain detected — 20-20-20 coming up.',
            'eye_strain':        'Screen fatigue building — rest incoming.',
            'leg_stillness':     'Legs haven\'t moved in a while — time to get up.',
            'shallow_breath':    'Shallow breathing detected — a stretch will help.',
            'wrist_tension':     'Wrist tension flagged — arm break incoming.',
            'general':           'Time for a short desk break.',
        }
        return messages.get(self._signal_name, 'An exercise break is coming up.')

    @staticmethod
    def _snooze_style(enabled: bool) -> str:
        if enabled:
            return """
                QPushButton {
                    background: rgba(255,255,255,0.12);
                    color: rgba(220, 210, 180, 0.9);
                    border: 1px solid rgba(200, 180, 120, 0.35);
                    border-radius: 15px;
                    font-size: 12px;
                    font-family: 'Georgia', serif;
                }
                QPushButton:hover {
                    background: rgba(255,255,255,0.22);
                    border-color: rgba(220,200,140,0.55);
                }
                QPushButton:pressed {
                    background: rgba(255,255,255,0.08);
                }
            """
        else:
            return """
                QPushButton {
                    background: transparent;
                    color: rgba(160, 150, 120, 0.5);
                    border: 1px solid rgba(160, 150, 120, 0.2);
                    border-radius: 15px;
                    font-size: 12px;
                    font-family: 'Georgia', serif;
                }
            """

    @staticmethod
    def _break_style() -> str:
        return """
            QPushButton {
                background: rgba(160, 210, 140, 0.22);
                color: rgba(200, 240, 180, 0.92);
                border: 1px solid rgba(140, 200, 110, 0.4);
                border-radius: 15px;
                font-size: 12px;
                font-family: 'Georgia', serif;
                letter-spacing: 0.3px;
            }
            QPushButton:hover {
                background: rgba(160, 210, 140, 0.38);
                border-color: rgba(140, 200, 110, 0.65);
                color: rgba(220, 255, 200, 0.98);
            }
            QPushButton:pressed {
                background: rgba(120, 180, 100, 0.3);
            }
        """

    def closeEvent(self, event):
        self._countdown_timer.stop()
        self._anim_timer.stop()
        self.dismissed.emit()
        super().closeEvent(event)


# ── Quick standalone test ─────────────────────────────────────────────────────

if __name__ == '__main__':
    app = QApplication(sys.argv)
    signal = sys.argv[1] if len(sys.argv) > 1 else 'forward_head'

    banner = WarningBanner(signal_name=signal)
    banner.unplug_requested.connect(lambda: (print('→ Unplug triggered'), app.quit()))
    banner.snoozed.connect(lambda s: print(f'→ Snoozed +{s}s'))
    banner.show()

    sys.exit(app.exec_())
