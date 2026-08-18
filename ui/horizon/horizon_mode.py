"""
horizon_mode.py — Canary Horizon Mode (20-20-20 Rule)
=====================================================
Dark, calm full-screen overlay.
"You've been focused for a while. Find something far away."
20 second countdown — shown at 20, 10, 0 only (minimises distraction).
Spacebar to exit after 10 seconds have elapsed.

Usage
-----
    mode = HorizonMode()
    mode.finished.connect(on_horizon_done)
    mode.start()
"""

import math, sys, random
from PyQt5.QtWidgets import QWidget, QApplication, QLabel
from PyQt5.QtCore    import Qt, QTimer, QRectF, QPointF, pyqtSignal
from PyQt5.QtGui     import (
    QPainter, QColor, QLinearGradient, QRadialGradient,
    QPen, QBrush, QPainterPath, QFont, QKeyEvent
)


# ── Gentle floating particle (subtle stars/motes in darkness) ─────────────────

class FloatMote:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.reset()

    def reset(self):
        self.x    = random.uniform(0, self.w)
        self.y    = random.uniform(0, self.h)
        self.vx   = random.uniform(-0.15, 0.15)
        self.vy   = random.uniform(-0.18, -0.05)
        self.r    = random.uniform(0.8, 2.2)
        self.life = random.uniform(0.3, 1.0)
        self.max_life = self.life
        self.decay= random.uniform(0.0008, 0.002)

    def update(self):
        self.x    += self.vx
        self.y    += self.vy
        self.life -= self.decay
        if self.life <= 0 or self.y < -10:
            self.reset()
            self.y = self.h + 5

    def draw(self, p: QPainter):
        frac  = self.life / self.max_life
        alpha = int(80 * frac)
        c = QColor(180, 170, 220, alpha)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawEllipse(QPointF(self.x, self.y), self.r, self.r)


# ── Main widget ───────────────────────────────────────────────────────────────

class HorizonMode(QWidget):
    """
    20-20-20 dark screen.
    Only shows digits at t=20, t=10, t=0 to avoid fixation.
    Spacebar exits after 10 s.
    """

    finished = pyqtSignal()

    # Suggestions cycle
    SUGGESTIONS = [
        'a clock on the wall',
        'a window',
        'a plant',
        'a distant object',
        'the far end of the room',
        'something at least 6 metres away',
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFocusPolicy(Qt.StrongFocus)

        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

        self._seconds_left  = 20
        self._can_exit      = False      # unlocks at t=10
        self._phase         = 'intro'    # intro → counting → done
        self._show_digit    = True       # True at t=20, 10, 0
        self._digit_fade    = 1.0        # 0→1 fade in, then 1→0 fade out
        self._digit_dir     = 1          # 1=fading in, -1=fading out
        self._intro_alpha   = 0.0
        self._suggestion    = random.choice(self.SUGGESTIONS)
        self._suggestion_idx= 0
        self._done_shown    = False
        self._glow_t        = 0.0
        self._escape_hint_a = 0.0

        self._motes: list[FloatMote] = []

        # Timers
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick_anim)

        self._second_timer = QTimer(self)
        self._second_timer.timeout.connect(self._tick_second)

    # ── Public ────────────────────────────────────────────────

    def start(self):
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)
        self.show()
        self.setFocus()

        self._seconds_left   = 20
        self._can_exit       = False
        self._phase          = 'intro'
        self._intro_alpha    = 0.0
        self._digit_fade     = 0.0
        self._digit_dir      = 1
        self._show_digit     = True
        self._done_shown     = False
        self._glow_t         = 0.0
        self._escape_hint_a  = 0.0
        self._suggestion     = random.choice(self.SUGGESTIONS)

        w, h = self.width(), self.height()
        self._motes = [FloatMote(w, h) for _ in range(38)]

        self._anim_timer.start(16)
        self._second_timer.start(1000)

    # ── Keyboard ─────────────────────────────────────────────

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key_Space, Qt.Key_Escape, Qt.Key_Return):
            if self._can_exit:
                self._finish()

    # ── Second tick ───────────────────────────────────────────

    def _tick_second(self):
        if self._phase == 'intro':
            self._phase = 'counting'

        self._seconds_left -= 1

        if self._seconds_left == 10:
            self._can_exit      = True
            self._escape_hint_a = 0.0

        # Show digit only at 20, 10, 0
        if self._seconds_left in (20, 10, 0):
            self._show_digit  = True
            self._digit_fade  = 0.0
            self._digit_dir   = 1

        if self._seconds_left <= 0:
            self._second_timer.stop()
            self._phase      = 'done'
            self._show_digit = True
            self._digit_fade = 0.0
            self._digit_dir  = 1
            QTimer.singleShot(2800, self._finish)

        # Rotate suggestion every 6 seconds
        if self._seconds_left in (14, 8):
            self._suggestion_idx = (self._suggestion_idx + 1) % len(self.SUGGESTIONS)
            self._suggestion = self.SUGGESTIONS[self._suggestion_idx]

    # ── Anim tick ────────────────────────────────────────────

    def _tick_anim(self):
        self._glow_t += 0.018

        # Intro fade-in
        if self._phase == 'intro' or self._intro_alpha < 1.0:
            self._intro_alpha = min(1.0, self._intro_alpha + 0.022)

        # Digit fade in then out
        if self._show_digit:
            if self._digit_dir == 1:
                self._digit_fade = min(1.0, self._digit_fade + 0.06)
                if self._digit_fade >= 1.0:
                    # Hold briefly then fade out (unless it's 0)
                    if self._seconds_left not in (0,):
                        QTimer.singleShot(900, self._start_digit_fadeout)
            elif self._digit_dir == -1:
                self._digit_fade = max(0.0, self._digit_fade - 0.04)
                if self._digit_fade <= 0:
                    self._show_digit = False

        # Escape hint pulse after can_exit
        if self._can_exit and self._seconds_left > 0:
            self._escape_hint_a = min(1.0, self._escape_hint_a + 0.015)

        for m in self._motes:
            m.update()

        self.update()

    def _start_digit_fadeout(self):
        self._digit_dir = -1

    # ── Finish ────────────────────────────────────────────────

    def _finish(self):
        self._anim_timer.stop()
        self._second_timer.stop()
        self.hide()
        self.finished.emit()

    # ── Paint ─────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # Global intro fade
        p.setOpacity(self._intro_alpha)

        # ── Background — very dark, slight cool tint ──────────
        bg = QLinearGradient(0, 0, 0, h)
        bg.setColorAt(0.0, QColor( 6,  8, 18))
        bg.setColorAt(1.0, QColor( 3,  4, 12))
        p.fillRect(0, 0, w, h, QBrush(bg))

        # ── Subtle horizon glow (distant light) ───────────────
        pulse = 0.85 + 0.15 * math.sin(self._glow_t * 0.7)
        horizon_y = h * 0.62
        horizon = QRadialGradient(w / 2, horizon_y, w * 0.45)
        horizon.setColorAt(0.0, QColor(60, 100, 160, int(22 * pulse)))
        horizon.setColorAt(1.0, QColor(0,   0,   0,  0))
        p.fillRect(0, 0, w, h, QBrush(horizon))

        # ── Floating motes ────────────────────────────────────
        for m in self._motes:
            m.draw(p)

        # ── Main copy ─────────────────────────────────────────
        if self._phase in ('counting', 'done') or self._intro_alpha > 0.5:
            self._draw_text(p, w, h)

        p.setOpacity(1.0)   # reset

    def _draw_text(self, p: QPainter, w: int, h: int):
        centre_y = h * 0.38

        # ── "You've been focused for a while." ────────────────
        p.save()
        font = QFont('Georgia', 22, QFont.Light)
        font.setItalic(True)
        p.setFont(font)
        p.setPen(QColor(160, 155, 190, int(200 * self._intro_alpha)))
        p.drawText(
            QRectF(0, centre_y - 90, w, 40),
            Qt.AlignCenter,
            'You\'ve been focused for a while.'
        )
        p.restore()

        # ── "Find something far away" ─────────────────────────
        p.save()
        font2 = QFont('Georgia', 36, QFont.Light)
        p.setFont(font2)
        pulse2 = 0.9 + 0.1 * math.sin(self._glow_t * 0.9)
        p.setPen(QColor(210, 205, 240, int(240 * self._intro_alpha * pulse2)))
        p.drawText(
            QRectF(0, centre_y - 5, w, 56),
            Qt.AlignCenter,
            'Find something far away.'
        )
        p.restore()

        # ── Suggestion ────────────────────────────────────────
        p.save()
        font3 = QFont('Georgia', 15)
        font3.setItalic(True)
        p.setFont(font3)
        p.setPen(QColor(120, 118, 155, int(160 * self._intro_alpha)))
        p.drawText(
            QRectF(0, centre_y + 75, w, 34),
            Qt.AlignCenter,
            f'Try: {self._suggestion}'
        )
        p.restore()

        # ── Countdown digit (shown only at 20, 10, 0) ─────────
        if self._show_digit and self._digit_fade > 0:
            digit_str = str(self._seconds_left) if self._seconds_left > 0 else 'Done  ✓'
            self._draw_digit(p, w, h, digit_str, self._digit_fade, centre_y)

        # ── Escape hint ───────────────────────────────────────
        if self._escape_hint_a > 0 and self._seconds_left > 0:
            p.save()
            hint_font = QFont('Courier New', 11)
            p.setFont(hint_font)
            p.setPen(QColor(100, 95, 130, int(110 * self._escape_hint_a)))
            p.drawText(
                QRectF(0, h * 0.88, w, 26),
                Qt.AlignCenter,
                'press  space  to return'
            )
            p.restore()

        # ── "Done?" prompt ────────────────────────────────────
        if self._phase == 'done':
            p.save()
            font4 = QFont('Georgia', 18, QFont.Light)
            p.setFont(font4)
            p.setPen(QColor(180, 175, 215, 200))
            p.drawText(
                QRectF(0, h * 0.74, w, 40),
                Qt.AlignCenter,
                'Done?  Eyes refreshed 🐦'
            )
            p.restore()

    def _draw_digit(self, p: QPainter, w: int, h: int,
                    text: str, alpha: float, centre_y: float):
        """Large centred countdown number with glow."""
        p.save()
        digit_y = centre_y + 130

        # Glow halo — sized and centred to comfortably contain both the
        # digit and the "seconds" label beneath it, with real margin
        glow_r = 190
        glow = QRadialGradient(w / 2, digit_y + 30, glow_r)
        glow.setColorAt(0.0, QColor(80, 100, 180, int(55 * alpha)))
        glow.setColorAt(1.0, QColor(0,   0,   0,  0))
        p.fillRect(int(w / 2 - glow_r), int(digit_y - glow_r + 30),
                   glow_r * 2, glow_r * 2, QBrush(glow))

        # Number — a 72pt font needs real vertical room (ascent+descent
        # comfortably exceeds the point size), so the box is taller than
        # it looks like it should need, to avoid clipping/cramping it.
        font = QFont('Georgia', 72, QFont.Thin)
        p.setFont(font)
        p.setPen(QColor(200, 195, 240, int(230 * alpha)))
        p.drawText(
            QRectF(0, digit_y - 70, w, 150),
            Qt.AlignCenter,
            text
        )

        # "seconds" subscript — pushed further down for real breathing
        # room below the number
        if text not in ('Done  ✓',):
            font2 = QFont('Georgia', 14, QFont.Light)
            font2.setItalic(True)
            p.setFont(font2)
            p.setPen(QColor(130, 125, 165, int(160 * alpha)))
            p.drawText(
                QRectF(0, digit_y + 105, w, 28),
                Qt.AlignCenter,
                'seconds'
            )

        p.restore()


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app = QApplication(sys.argv)
    mode = HorizonMode()
    mode.finished.connect(lambda: (print('Horizon mode done'), app.quit()))
    mode.start()
    sys.exit(app.exec_())