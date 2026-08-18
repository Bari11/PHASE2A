"""
logo_intro.py — Canary Horizon Mode: Logo Intro
================================================================
Brief, calm-ending launch animation for Horizon Mode (~5s). Its only
job is to signal "Horizon Mode has started" — it is not meant to
entertain, and it deliberately settles into complete stillness before
handing off to the real calm screen (ui/horizon/horizon_mode.py).

Sequence
--------
  0.0–0.8s   H R I Z N drop from above into place, leaving gaps for O's
  0.8–1.6s   Both O's (with eyes already inside) drop in, bounce twice
  1.6–3.0s   Pupils in both O's look left → right → front
  3.0–4.4s   The O's swap positions along a semicircular arc above the
             other letters, then settle
  4.4s+      Everything is completely still — this is the moment the
             screen should read as "calm," right before HorizonMode
             takes over

No image assets are used anywhere in this file — every visual element
(letters, O's, eyes, pupils, glow, reflection) is vector-drawn with
QPainter, the same technique proven out in ui/unplug/opening_animation.py's
"UNPLUG" wordmark. This avoids depending on hand-traced art entirely.

Structure
---------
    Letter        — one static neon-outline glyph (H, R, I, Z, N)
    OLetter       — the animated O: circle + two Eye children
    Eye / Pupil   — small vector shapes, gaze offset animates independently
    AnimationController — phase/timing math, no drawing
    LogoIntro(QWidget)   — top-level widget, owns the 60 FPS timer

Usage
-----
    intro = LogoIntro()
    intro.finished.connect(show_calm_horizon_screen)
    intro.start()
"""

import math
import sys

from PyQt5.QtWidgets import QWidget, QApplication
from PyQt5.QtCore import Qt, QTimer, QPointF, QRectF, pyqtSignal
from PyQt5.QtGui import (
    QPainter, QColor, QLinearGradient, QRadialGradient,
    QPen, QBrush, QPainterPath, QFont, QTransform,
)


# ─────────────────────────────────────────────────────────────────────────────
#  Easing helpers
# ─────────────────────────────────────────────────────────────────────────────

def ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def ease_out_bounce(t: float) -> float:
    """Two-bounce settle, used when the O's land."""
    t = max(0.0, min(1.0, t))
    n1, d1 = 7.5625, 2.75
    if t < 1 / d1:
        return n1 * t * t
    elif t < 2 / d1:
        t -= 1.5 / d1
        return n1 * t * t + 0.75
    elif t < 2.5 / d1:
        t -= 2.25 / d1
        return n1 * t * t + 0.9375
    else:
        t -= 2.625 / d1
        return n1 * t * t + 0.984375


def ease_in_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return 4 * t * t * t
    return 1 - ((-2 * t + 2) ** 3) / 2


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


# ─────────────────────────────────────────────────────────────────────────────
#  Letter — a single static neon-outline glyph
# ─────────────────────────────────────────────────────────────────────────────

class Letter:
    """One hollow neon-tube letter (stroke only, transparent interior) —
    same rendering technique as Unplug's "UNPLUG" wordmark."""

    def __init__(self, char: str, color: QColor, font: QFont):
        self.char = char
        self.color = color
        self.font = font
        self.drop_progress = 1.0   # 0 = above screen, 1 = in place
        self.alpha = 1.0

    def glyph_path(self, x: float, baseline_y: float) -> QPainterPath:
        path = QPainterPath()
        path.addText(x, baseline_y, self.font, self.char)
        return path

    def draw(self, p: QPainter, x: float, rest_baseline_y: float, drop_from_y: float):
        y = lerp(drop_from_y, rest_baseline_y, ease_out_cubic(self.drop_progress))
        path = self.glyph_path(x, y)
        c = self.color
        core = QColor(c.red(), c.green(), c.blue(), int(255 * self.alpha))
        halo = QColor(c.red(), c.green(), c.blue(), int(220 * self.alpha))

        p.setBrush(Qt.NoBrush)
        for width, alpha_mult in ((18, 0.10), (12, 0.18), (7, 0.30), (3.5, 0.48)):
            p.setPen(QPen(QColor(halo.red(), halo.green(), halo.blue(),
                                  int(halo.alpha() * alpha_mult)),
                          width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
        p.setPen(QPen(core, 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)


# ─────────────────────────────────────────────────────────────────────────────
#  Pupil / Eye — small vector shapes, independent gaze animation
# ─────────────────────────────────────────────────────────────────────────────

class Pupil:
    RX, RY = 6.0, 8.0     # nearly fills the eye, matching the reference
    MAX_OFFSET = 2.5

    def __init__(self):
        self.gaze = 0.0   # -1 = full left, 0 = front, +1 = full right

    def draw(self, p: QPainter, center: QPointF):
        dx = self.gaze * self.MAX_OFFSET
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 15, 20, 255))
        p.drawEllipse(QPointF(center.x() + dx, center.y()), self.RX, self.RY)


class Eye:
    RX, RY = 8.0, 11.0    # taller "almond" shape, not a plain circle

    def __init__(self, offset_x: float):
        self.offset_x = offset_x   # position relative to the O's own centre
        self.pupil = Pupil()

    def draw(self, p: QPainter, o_center: QPointF):
        center = QPointF(o_center.x() + self.offset_x, o_center.y())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(250, 250, 255, 255))
        p.drawEllipse(center, self.RX, self.RY)
        self.pupil.draw(p, center)


# ─────────────────────────────────────────────────────────────────────────────
#  OLetter — the animated O: circle body + two eyes
# ─────────────────────────────────────────────────────────────────────────────

class OLetter:
    RADIUS = 46

    def __init__(self, color: QColor):
        self.color = color
        self.alpha = 1.0
        self.eyes = [Eye(-15), Eye(15)]
        # Current on-screen position (updated every frame by the controller)
        self.x = 0.0
        self.y = 0.0

    def set_gaze(self, gaze: float):
        for eye in self.eyes:
            eye.pupil.gaze = gaze

    def draw(self, p: QPainter):
        c = self.color
        core = QColor(c.red(), c.green(), c.blue(), int(255 * self.alpha))
        halo = QColor(c.red(), c.green(), c.blue(), int(220 * self.alpha))
        center = QPointF(self.x, self.y)

        p.setBrush(Qt.NoBrush)
        for width, alpha_mult in ((18, 0.10), (12, 0.18), (7, 0.30), (3.5, 0.48)):
            p.setPen(QPen(QColor(halo.red(), halo.green(), halo.blue(),
                                  int(halo.alpha() * alpha_mult)),
                          width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawEllipse(center, self.RADIUS, self.RADIUS)
        p.setPen(QPen(core, 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawEllipse(center, self.RADIUS, self.RADIUS)

        for eye in self.eyes:
            eye.draw(p, center)


# ─────────────────────────────────────────────────────────────────────────────
#  AnimationController — pure timing/phase math, no painting
# ─────────────────────────────────────────────────────────────────────────────

class AnimationController:
    DROP = 0
    O_DROP = 1
    GAZE = 2
    SWAP = 3
    STILL = 4
    DONE = 5

    DUR_DROP = 800
    DUR_O_DROP = 800
    DUR_GAZE = 1400
    DUR_SWAP = 1400
    DUR_STILL = 600       # brief hold on stillness before handing off
    TOTAL_MS = DUR_DROP + DUR_O_DROP + DUR_GAZE + DUR_SWAP + DUR_STILL

    def __init__(self):
        self.elapsed_ms = 0
        self._t1 = self.DUR_DROP
        self._t2 = self._t1 + self.DUR_O_DROP
        self._t3 = self._t2 + self.DUR_GAZE
        self._t4 = self._t3 + self.DUR_SWAP
        self._t5 = self._t4 + self.DUR_STILL

    def advance(self, dt_ms: float):
        self.elapsed_ms += dt_ms

    @property
    def phase(self) -> int:
        e = self.elapsed_ms
        if e < self._t1:
            return self.DROP
        if e < self._t2:
            return self.O_DROP
        if e < self._t3:
            return self.GAZE
        if e < self._t4:
            return self.SWAP
        if e < self._t5:
            return self.STILL
        return self.DONE

    @property
    def finished(self) -> bool:
        return self.phase == self.DONE

    def local_t(self, phase_start: float, duration: float) -> float:
        return max(0.0, min(1.0, (self.elapsed_ms - phase_start) / duration))

    def drop_progress(self) -> float:
        return ease_out_cubic(self.local_t(0, self.DUR_DROP))

    def o_drop_progress(self) -> float:
        return ease_out_bounce(self.local_t(self._t1, self.DUR_O_DROP))

    def gaze_value(self) -> float:
        """-1 (left) -> +1 (right) -> 0 (front), across the GAZE phase."""
        t = self.local_t(self._t2, self.DUR_GAZE)
        # three equal segments: left, right, front
        if t < 1 / 3:
            return lerp(0.0, -1.0, ease_in_out_cubic(t / (1 / 3)))
        elif t < 2 / 3:
            local = (t - 1 / 3) / (1 / 3)
            return lerp(-1.0, 1.0, ease_in_out_cubic(local))
        else:
            local = (t - 2 / 3) / (1 / 3)
            return lerp(1.0, 0.0, ease_in_out_cubic(local))

    def swap_progress(self) -> float:
        return ease_in_out_cubic(self.local_t(self._t3, self.DUR_SWAP))


# ─────────────────────────────────────────────────────────────────────────────
#  LogoIntro — top-level widget
# ─────────────────────────────────────────────────────────────────────────────

class LogoIntro(QWidget):
    """
    HORIZON logo launch animation. Ends in complete stillness and emits
    `finished` — the caller is expected to follow it with the calm
    HorizonMode overlay (ui/horizon/horizon_mode.py), e.g.:

        intro = LogoIntro()
        intro.finished.connect(lambda: HorizonMode().start())
        intro.start()
    """

    finished = pyqtSignal()
    TICK_MS = 16

    LETTER_ORDER = ['H', 'O', 'R', 'I', 'Z', 'O', 'N']
    COLORS = [
        QColor(150, 60, 230),   # H  purple
        QColor(50, 110, 230),   # O  blue
        QColor(40, 190, 210),   # R  cyan
        QColor(90, 210, 90),    # I  green
        QColor(220, 200, 50),   # Z  yellow
        QColor(230, 140, 30),   # O  orange
        QColor(220, 60, 60),    # N  red
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)

        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

        self._font = QFont('Arial', 64, QFont.Black)
        self._font.setLetterSpacing(QFont.PercentageSpacing, 105)

        self._controller = AnimationController()
        self._letters = {}    # index -> Letter, for H R I Z N only
        self._o_letters = []  # the two OLetter instances, in logo order

        for i, ch in enumerate(self.LETTER_ORDER):
            if ch == 'O':
                self._o_letters.append(OLetter(self.COLORS[i]))
            else:
                self._letters[i] = Letter(ch, self.COLORS[i], self._font)

        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._on_tick)

        self._layout_cache = None   # computed once we know widget size

    # ── Public API ──────────────────────────────────────────────

    def start(self):
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)
        self._controller = AnimationController()
        self._layout_cache = None
        self.show()
        self.raise_()
        self._tick_timer.start(self.TICK_MS)

    # ── Layout ──────────────────────────────────────────────────

    def _compute_layout(self):
        """Measures glyph widths once and lays out the whole wordmark
        centred on screen, returning per-letter x positions and the O
        slot rectangles."""
        w, h = self.width(), self.height()
        fm_widths = []
        from PyQt5.QtGui import QFontMetrics
        fm = QFontMetrics(self._font)
        gap = 14
        for ch in self.LETTER_ORDER:
            if ch == 'O':
                fm_widths.append(OLetter.RADIUS * 2)
            else:
                fm_widths.append(fm.horizontalAdvance(ch))

        total_w = sum(fm_widths) + gap * (len(self.LETTER_ORDER) - 1)
        start_x = w / 2 - total_w / 2
        baseline_y = h / 2

        xs = []
        cursor = start_x
        for lw in fm_widths:
            xs.append(cursor)
            cursor += lw + gap

        self._layout_cache = {
            'xs': xs, 'baseline_y': baseline_y, 'widths': fm_widths,
        }

    # ── Tick / lifecycle ────────────────────────────────────────

    def _on_tick(self):
        self._controller.advance(self.TICK_MS)
        if self._controller.finished:
            self._finish()
            return
        self.update()

    def _finish(self):
        self._tick_timer.stop()
        self.hide()
        self.finished.emit()

    def closeEvent(self, event):
        self._tick_timer.stop()
        super().closeEvent(event)

    # ── Paint ───────────────────────────────────────────────────

    def paintEvent(self, event):
        if self._layout_cache is None:
            self._compute_layout()

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        p.fillRect(0, 0, w, h, QColor(0, 0, 0))

        ctrl = self._controller
        xs = self._layout_cache['xs']
        baseline_y = self._layout_cache['baseline_y']
        widths = self._layout_cache['widths']

        drop_from_y = -150.0
        o_drop_from_y = baseline_y - 220.0

        # ── Resolve current position of every slot ──────────────
        # H R I Z N: simple drop-in, done once and then static.
        for i, letter in self._letters.items():
            letter.drop_progress = 1.0 if ctrl.phase != AnimationController.DROP \
                else ctrl.drop_progress()

        # Both O's: drop + bounce, then (later) swap along an arc.
        o_indices = [i for i, ch in enumerate(self.LETTER_ORDER) if ch == 'O']
        o_center_x = [xs[i] + widths[i] / 2 for i in o_indices]
        # Rest the O's bottom edge exactly on the baseline (same as every
        # other letter's glyph baseline) so it doesn't dip below the
        # mirror line into its own reflection.
        o_center_y = baseline_y - OLetter.RADIUS

        phase = ctrl.phase
        if phase <= AnimationController.DROP:
            # O's haven't appeared yet — keep them off-screen (invisible)
            for o in self._o_letters:
                o.alpha = 0.0
        elif phase == AnimationController.O_DROP:
            t = ctrl.o_drop_progress()
            for idx, o in enumerate(self._o_letters):
                o.alpha = 1.0
                o.x = o_center_x[idx]
                o.y = lerp(o_drop_from_y, o_center_y, t)
        elif phase in (AnimationController.GAZE,):
            for idx, o in enumerate(self._o_letters):
                o.alpha = 1.0
                o.x = o_center_x[idx]
                o.y = o_center_y
            gaze = ctrl.gaze_value()
            for o in self._o_letters:
                o.set_gaze(gaze)
        elif phase == AnimationController.SWAP:
            t = ctrl.swap_progress()
            left_x, right_x = o_center_x[0], o_center_x[1]
            radius = abs(right_x - left_x) / 2
            mid_x = (left_x + right_x) / 2

            def arc_pos(start_x, end_x, tt, height_factor):
                angle = lerp(math.pi, 0.0, tt) if start_x < end_x else lerp(0.0, math.pi, tt)
                x = lerp(start_x, end_x, tt)
                arc_y = -math.sin(tt * math.pi) * radius * height_factor
                return x, o_center_y + arc_y

            # Left O arcs over to the right slot; right O arcs over to the
            # left slot, at a slightly different height so they don't
            # visually collide at the crossing point.
            lx, ly = arc_pos(left_x, right_x, t, 1.0)
            rx, ry = arc_pos(right_x, left_x, t, 1.28)
            self._o_letters[0].x, self._o_letters[0].y = lx, ly
            self._o_letters[1].x, self._o_letters[1].y = rx, ry
            for o in self._o_letters:
                o.alpha = 1.0
                o.set_gaze(0.0)
        else:  # STILL / DONE
            # Settled in swapped positions, looking front, completely static.
            right_x, left_x = o_center_x[0], o_center_x[1]
            self._o_letters[0].x, self._o_letters[0].y = o_center_x[1], o_center_y
            self._o_letters[1].x, self._o_letters[1].y = o_center_x[0], o_center_y
            for o in self._o_letters:
                o.alpha = 1.0
                o.set_gaze(0.0)

        # ── Draw main logo ───────────────────────────────────────
        for i, letter in self._letters.items():
            letter.draw(p, xs[i], baseline_y, drop_from_y)
        for o in self._o_letters:
            o.draw(p)

        # ── Reflection: flipped copy, faded, generated live — starts
        #    exactly at the baseline so it touches the logo with no gap
        p.save()
        p.setOpacity(0.34)
        transform = QTransform()
        transform.translate(0, 2 * baseline_y)
        transform.scale(1, -1)
        p.setTransform(transform, combine=True)

        for i, letter in self._letters.items():
            letter.draw(p, xs[i], baseline_y, drop_from_y)
        for o in self._o_letters:
            o.draw(p)
        p.restore()

        # Fade the reflection out toward the bottom of the screen
        fade_rect = QRectF(0, baseline_y, w, h - baseline_y)
        fade_grad = QLinearGradient(0, fade_rect.top(), 0, fade_rect.bottom())
        fade_grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        fade_grad.setColorAt(1.0, QColor(0, 0, 0, 255))
        p.fillRect(fade_rect, QBrush(fade_grad))

        # ── Glowing baseline line — the seam where logo meets
        #    reflection, touching both, coloured across the rainbow ──
        line_left = xs[0] - 20
        line_right = xs[-1] + widths[-1] + 20
        rainbow = QLinearGradient(line_left, 0, line_right, 0)
        n = len(self.COLORS)
        for i, c in enumerate(self.COLORS):
            rainbow.setColorAt(i / (n - 1), c)

        for width, alpha_mult in ((16, 0.10), (9, 0.20), (4, 0.35)):
            pen = QPen(QBrush(rainbow), width, Qt.SolidLine, Qt.RoundCap)
            p.setOpacity(alpha_mult)
            p.setPen(pen)
            p.drawLine(QPointF(line_left, baseline_y), QPointF(line_right, baseline_y))
        p.setOpacity(1.0)
        pen = QPen(QBrush(rainbow), 1.6, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(line_left, baseline_y), QPointF(line_right, baseline_y))


if __name__ == '__main__':
    app = QApplication(sys.argv)
    intro = LogoIntro()
    intro.finished.connect(lambda: (print('Logo intro done — handing off to calm screen'), app.quit()))
    intro.start()
    sys.exit(app.exec_())