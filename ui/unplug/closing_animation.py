"""
closing_animation.py — Canary Unplug Mode: Closing Animation
================================================================
Plays once the break/exercises are finished. Replaces the old
energy_restoration.py / energy_restored.py (both deleted — this
supersedes them both; those were earlier, more dramatic takes on the
same "reconnection" beat — gold lightning bolts, "WE ARE BACK" text,
branching purple lightning arcs — which we deliberately moved away
from in favour of something calmer and literal).

Design intent: elegant and premium, not a special-effects sting. The
plug and socket slide back together (mirroring the Opening Animation's
disconnect, in reverse), click into place, and that instant produces a
brief, restrained burst — one soft flash, one expanding ring, a few
short fading rays. No multi-color sparkle, no branching bolts, no
scattering particles on the hardware itself. ~2 seconds total.

Sequence (~2.0s)
-----------------
  1. SLIDE   (0.0 – 0.9s)  — plug and socket ease toward each other from
                              full separation to touching, cable
                              stretching to the screen edges throughout
                              (identical technique to OpeningAnimation).
  2. SETTLE  (0.9 – 1.15s) — at the exact instant they touch: a soft
                              violet-white flash, one thin expanding
                              ring, a handful of short rays — all fading
                              within ~450ms. Concurrently, the separate
                              Plug/Socket crossfade into the merged
                              ConnectedCapsule (reverse of the Opening
                              Animation's morph).
  3. HOLD    (1.15 – 1.35s) — a brief, calm moment on the fully connected
                              capsule, nothing moving.
  4. FADE    (1.35 – 2.0s)  — fades to black, then emits `animation_done`
                              for the caller to reveal the main interface.

Reuses Plug / Socket / ConnectedCapsule directly from
ui.unplug.opening_animation — same real artwork, same scale, so the
open → close loop feels like one continuous object, not two different
effects stitched together.

Usage
-----
    anim = ClosingAnimation()
    anim.animation_done.connect(show_main_interface)
    anim.start()
"""

import math
import os
import sys

from PyQt5.QtWidgets import QWidget, QApplication
from PyQt5.QtCore import Qt, QTimer, QPointF, QRectF, pyqtSignal
from PyQt5.QtGui import QPainter, QColor, QRadialGradient, QPen, QBrush

from ui.unplug.opening_animation import (
    Plug, Socket, ConnectedCapsule, NEON_COLOR,
    ease_out_cubic, ease_in_out_cubic, lerp,
)

try:
    from PyQt5.QtMultimedia import QSoundEffect
    from PyQt5.QtCore import QUrl
    _HAS_QT_MULTIMEDIA = True
except Exception:
    _HAS_QT_MULTIMEDIA = False


# ─────────────────────────────────────────────────────────────────────────────
#  Sound hook (placeholder — safe no-op if file/QtMultimedia is missing)
# ─────────────────────────────────────────────────────────────────────────────

class _ClickSound:
    PATH = os.path.join('assets', 'sounds', 'reconnect_click.wav')

    def __init__(self):
        self._fx = None
        if _HAS_QT_MULTIMEDIA:
            try:
                if os.path.isfile(self.PATH):
                    self._fx = QSoundEffect()
                    self._fx.setSource(QUrl.fromLocalFile(os.path.abspath(self.PATH)))
                    self._fx.setVolume(0.5)
            except Exception:
                self._fx = None

    def play(self):
        try:
            if self._fx is not None:
                self._fx.play()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  ReconnectBurst — soft flash + one ring + a few short rays
# ─────────────────────────────────────────────────────────────────────────────

class ReconnectBurst:
    """
    The entire "connection made" beat, deliberately restrained:
      - one soft flash (violet-white, peaks instantly, fades fast)
      - one expanding ring (not concentric multiples — that reads as
        a shockwave/explosion)
      - a handful of short rays that fade quickly (long bright rays
        read as a "magic circle", which we're avoiding)

    Driven by an externally-supplied progress value (0..1 across its
    whole lifetime) rather than owning its own clock, so it stays in
    lockstep with AnimationController.
    """

    RING_MAX_RADIUS = 230
    RAY_COUNT = 7
    RAY_MAX_LEN = 85

    def draw(self, p: QPainter, origin: QPointF, progress: float):
        if progress >= 1.0:
            return
        p.save()

        # Soft flash — peaks immediately, fades out by ~30% of the burst
        flash_t = min(1.0, progress / 0.3)
        flash_alpha = (1.0 - flash_t) ** 1.6
        if flash_alpha > 0.01:
            r = 70
            glow = QRadialGradient(origin, r)
            glow.setColorAt(0.0, QColor(240, 225, 255, int(220 * flash_alpha)))
            glow.setColorAt(0.5, QColor(NEON_COLOR.red(), NEON_COLOR.green(),
                                        NEON_COLOR.blue(), int(120 * flash_alpha)))
            glow.setColorAt(1.0, QColor(NEON_COLOR.red(), NEON_COLOR.green(),
                                        NEON_COLOR.blue(), 0))
            p.setBrush(QBrush(glow))
            p.setPen(Qt.NoPen)
            p.drawEllipse(origin, r, r)

        # One expanding ring
        ring_t = ease_out_cubic(progress)
        ring_alpha = (1.0 - progress) ** 1.4
        if ring_alpha > 0.01:
            radius = 4 + ring_t * self.RING_MAX_RADIUS
            c = NEON_COLOR
            pen = QPen(QColor(c.red(), c.green(), c.blue(), int(200 * ring_alpha)),
                       2.4, Qt.SolidLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(origin, radius, radius)

        # A few short, fast-fading rays
        ray_t = min(1.0, progress / 0.4)
        ray_len = ease_out_cubic(ray_t) * self.RAY_MAX_LEN
        ray_alpha = (1.0 - min(1.0, progress / 0.65)) ** 1.5
        if ray_alpha > 0.01 and ray_len > 0.5:
            c = NEON_COLOR
            pen = QPen(QColor(c.red(), c.green(), c.blue(), int(190 * ray_alpha)),
                       1.8, Qt.SolidLine, Qt.RoundCap)
            p.setPen(pen)
            for i in range(self.RAY_COUNT):
                angle = (math.tau / self.RAY_COUNT) * i + 0.3
                dx, dy = math.cos(angle), math.sin(angle)
                start = QPointF(origin.x() + dx * 10, origin.y() + dy * 10)
                end = QPointF(origin.x() + dx * (10 + ray_len),
                              origin.y() + dy * (10 + ray_len))
                p.drawLine(start, end)

        p.restore()


# ─────────────────────────────────────────────────────────────────────────────
#  AnimationController — pure timing/phase math, no painting
# ─────────────────────────────────────────────────────────────────────────────

class AnimationController:
    SLIDE = 0
    SETTLE = 1
    HOLD = 2
    FADE = 3
    DONE = 4

    DUR_SLIDE = 900
    DUR_SETTLE = 250
    DUR_HOLD = 200
    DUR_FADE = 650
    TOTAL_MS = DUR_SLIDE + DUR_SETTLE + DUR_HOLD + DUR_FADE   # = 2000ms

    MAX_SEPARATION = 0.20   # matches OpeningAnimation, for visual continuity
    BURST_MS = 450          # the burst's own lifetime, starts at the SLIDE→SETTLE instant

    def __init__(self):
        self.elapsed_ms = 0
        self.burst_fired = False
        self._t1 = self.DUR_SLIDE
        self._t2 = self._t1 + self.DUR_SETTLE
        self._t3 = self._t2 + self.DUR_HOLD
        self._t4 = self._t3 + self.DUR_FADE

    def advance(self, dt_ms: float):
        self.elapsed_ms += dt_ms

    @property
    def phase(self) -> int:
        e = self.elapsed_ms
        if e < self._t1:
            return self.SLIDE
        if e < self._t2:
            return self.SETTLE
        if e < self._t3:
            return self.HOLD
        if e < self._t4:
            return self.FADE
        return self.DONE

    @property
    def finished(self) -> bool:
        return self.phase == self.DONE

    @property
    def just_connected(self) -> bool:
        """True exactly once, the tick the plug/socket finish sliding
        together — this is what fires the burst and the click sound."""
        return (not self.burst_fired) and self.elapsed_ms >= self._t1

    # ── Derived values ──────────────────────────────────────────

    def slide_progress(self) -> float:
        """0 = fully separated, 1 = touching. Eases INTO the connection
        (decelerating, like a real plug settling into a socket)."""
        if self.elapsed_ms >= self._t1:
            return 1.0
        return ease_out_cubic(self.elapsed_ms / self.DUR_SLIDE)

    def morph_progress(self) -> float:
        """1 = fully separate hardware, 0 = fully merged capsule —
        reverse of OpeningAnimation's morph, since this animation ends
        connected rather than starts connected."""
        if self.elapsed_ms < self._t1:
            return 1.0
        if self.elapsed_ms >= self._t2:
            return 0.0
        local_t = (self.elapsed_ms - self._t1) / self.DUR_SETTLE
        return 1.0 - ease_in_out_cubic(local_t)

    def burst_progress(self) -> float:
        """0..1 across the burst's own (short) lifetime, starting the
        instant the plug/socket touch."""
        if self.elapsed_ms < self._t1:
            return 0.0
        local_t = (self.elapsed_ms - self._t1) / self.BURST_MS
        return min(1.0, local_t)

    def fade_progress(self) -> float:
        if self.elapsed_ms < self._t3:
            return 0.0
        if self.elapsed_ms >= self._t4:
            return 1.0
        return ease_in_out_cubic((self.elapsed_ms - self._t3) / self.DUR_FADE)


# ─────────────────────────────────────────────────────────────────────────────
#  ClosingAnimation — top-level widget
# ─────────────────────────────────────────────────────────────────────────────

class ClosingAnimation(QWidget):
    """
    The break-is-over reconnect animation. Ends on a solid black screen
    and emits `animation_done` — the caller reveals the main interface
    from there, e.g.:

        anim = ClosingAnimation()
        anim.animation_done.connect(show_main_interface)
        anim.start()
    """

    animation_done = pyqtSignal()
    TICK_MS = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)

        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

        self._controller = AnimationController()
        self._capsule = ConnectedCapsule()
        self._plug = Plug()
        self._socket = Socket()
        self._burst = ReconnectBurst()
        self._sound = _ClickSound()

        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._on_tick)

    # ── Public API ──────────────────────────────────────────────

    def start(self):
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)
        self._controller = AnimationController()
        self.show()
        self.raise_()
        self._tick_timer.start(self.TICK_MS)

    # ── Tick / lifecycle ────────────────────────────────────────

    def _on_tick(self):
        self._controller.advance(self.TICK_MS)
        if self._controller.just_connected:
            self._controller.burst_fired = True
            self._sound.play()
        if self._controller.finished:
            self._finish()
            return
        self.update()

    def _finish(self):
        self._tick_timer.stop()
        self.hide()
        self.animation_done.emit()

    def closeEvent(self, event):
        self._tick_timer.stop()
        super().closeEvent(event)

    # ── Paint ───────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        hw_y = cy + h * 0.16   # same hardware row as OpeningAnimation

        ctrl = self._controller
        p.fillRect(0, 0, w, h, QColor(4, 2, 8))

        slide = ctrl.slide_progress()
        morph = ctrl.morph_progress()
        fade = ctrl.fade_progress()
        alpha_mult = 1.0 - fade

        # ── Positions: start fully separated, ease together to touch ──
        max_sep_px = w * ctrl.MAX_SEPARATION
        gap = max_sep_px * (1.0 - slide)
        plug_tip_x = cx - gap
        socket_tip_x = cx + gap

        self._plug.glow_alpha = alpha_mult * morph
        self._socket.glow_alpha = alpha_mult * morph
        self._capsule.glow_alpha = alpha_mult * (1.0 - morph)
        self._plug.breathe = 1.0
        self._socket.breathe = 1.0
        self._capsule.breathe = 1.0

        if morph < 1.0:
            self._capsule.draw(p, cx, hw_y, w)
        if morph > 0.0:
            self._plug.draw(p, plug_tip_x, hw_y, 0.0)
            self._socket.draw(p, socket_tip_x, hw_y, w)

        # ── Burst, exactly at the connection point ───────────────
        burst_t = ctrl.burst_progress()
        if ctrl.burst_fired and burst_t < 1.0:
            self._burst.draw(p, QPointF(cx, hw_y), burst_t)

        # ── Fade to black ─────────────────────────────────────────
        if fade > 0:
            p.fillRect(0, 0, w, h, QColor(4, 2, 8, int(255 * fade)))


if __name__ == '__main__':
    app = QApplication(sys.argv)
    anim = ClosingAnimation()
    anim.animation_done.connect(lambda: (print('Closing animation done — revealing main interface'), app.quit()))
    anim.start()
    sys.exit(app.exec_())