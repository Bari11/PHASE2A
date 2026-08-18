"""
opening_animation.py — Canary Unplug Mode: Opening Animation
================================================================
Cinematic intro that plays the moment Unplug Mode is triggered.

Visual reference: dark background, glowing purple wire, plug + socket,
centred "UNPLUG" neon wordmark.

Sequence (≈7s total)
---------------------
  1. HOLD        — plug + socket connected, wire glowing softly,
                    "UNPLUG" centred, subtle breathing pulse.
  2. DISCONNECT  — plug eases left, socket eases right, wire stretches
                    and sags naturally. "UN" travels with the plug,
                    "PLUG" travels with the socket.
  3. SPARK       — at the exact moment of separation: a small purple
                    spark + a handful of purple particles. Subtle,
                    not explosive.
  4. FADE        — wire glow fades out, whole screen fades to black.

The animation stops on a black screen and emits `animation_done`.
It does NOT chain into the exercise environment — that hookup is a
later phase (see bottom of this file for the intended extension point).

Structure (mirrors a component-based layout even though this is one
PyQt5 widget file, per project convention — see slow_unplug.py /
unplug_animation.py for the same pattern):

    AnimationController   — phase/timing state machine, pure math, no drawing
    Wire                   — draws the connecting cable (glow + sag/stretch)
    Plug                   — left connector half
    Socket                 — right connector half
    UnplugText             — "UN" / "PLUG" wordmark halves
    Particle / SparkEffect — the disconnect spark burst
    OpeningAnimation        — top-level QWidget, owns a timer, composes
                              the pieces above and paints them each tick.

Usage
-----
    anim = OpeningAnimation()
    anim.animation_done.connect(on_black_screen_reached)
    anim.start()
"""

import math
import os
import random
import sys

from PyQt5.QtWidgets import QWidget, QApplication
from PyQt5.QtCore import Qt, QTimer, QPointF, QRectF, pyqtSignal
from PyQt5.QtGui import (
    QPainter, QColor, QRadialGradient, QLinearGradient,
    QPen, QBrush, QPainterPath, QFont, QPixmap,
)

# Optional sound support — degrades silently if QtMultimedia or the
# actual sound files aren't available. See _SoundHooks below.
try:
    from PyQt5.QtMultimedia import QSoundEffect
    from PyQt5.QtCore import QUrl
    _HAS_QT_MULTIMEDIA = True
except Exception:
    _HAS_QT_MULTIMEDIA = False


# ─────────────────────────────────────────────────────────────────────────────
#  Easing helpers
# ─────────────────────────────────────────────────────────────────────────────

def ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return 4 * t * t * t
    return 1 - ((-2 * t + 2) ** 3) / 2


def ease_out_quad(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) * (1 - t)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(lerp(c1.red(), c2.red(), t)),
        int(lerp(c1.green(), c2.green(), t)),
        int(lerp(c1.blue(), c2.blue(), t)),
        int(lerp(c1.alpha(), c2.alpha(), t)),
    )


def rounded_polygon_path(points, radius: float) -> QPainterPath:
    """
    Builds a closed path through `points` with every corner filleted by
    `radius` — i.e. a real bent-neon-tube silhouette instead of sharp
    mitred corners, matching the reference art's soft, continuous curves.
    """
    path = QPainterPath()
    n = len(points)

    def toward(a: QPointF, b: QPointF, dist: float) -> QPointF:
        dx, dy = b.x() - a.x(), b.y() - a.y()
        length = math.hypot(dx, dy) or 1e-6
        d = min(dist, length / 2.0)
        return QPointF(a.x() + dx / length * d, a.y() + dy / length * d)

    entries, exits = [], []
    for i in range(n):
        prev_pt = points[i - 1]
        cur = points[i]
        nxt = points[(i + 1) % n]
        entries.append(toward(cur, prev_pt, radius))
        exits.append(toward(cur, nxt, radius))

    path.moveTo(exits[-1])
    for i in range(n):
        path.lineTo(entries[i])
        path.quadTo(points[i], exits[i])
    path.closeSubpath()
    return path


# ─────────────────────────────────────────────────────────────────────────────
#  Hardware artwork (real reference images, not hand-drawn paths)
# ─────────────────────────────────────────────────────────────────────────────

_ASSET_DIR = os.path.join('assets', 'unplug')
_PIXMAP_CACHE = {}


def _load_pixmap(filename: str) -> QPixmap:
    path = os.path.join(_ASSET_DIR, filename)
    if path not in _PIXMAP_CACHE:
        _PIXMAP_CACHE[path] = QPixmap(path)
    return _PIXMAP_CACHE[path]


# ─────────────────────────────────────────────────────────────────────────────
#  Sound hooks (placeholders — safe no-ops if files/QtMultimedia are missing)
# ─────────────────────────────────────────────────────────────────────────────

class _SoundHooks:
    """
    Placeholder hooks for the two sound cues this animation needs:
      - disconnect_sound : plays as the plug/socket separate
      - spark_sound       : plays at the exact spark moment

    Drop matching files into canary_app/assets/sounds/ and they'll be
    picked up automatically. Until then, this fails silently — the
    animation is fully functional without audio.
    """

    DISCONNECT_SOUND_PATH = os.path.join('assets', 'sounds', 'unplug_disconnect.wav')
    SPARK_SOUND_PATH = os.path.join('assets', 'sounds', 'unplug_spark.wav')

    def __init__(self):
        self._disconnect_fx = None
        self._spark_fx = None
        if _HAS_QT_MULTIMEDIA:
            self._disconnect_fx = self._load(self.DISCONNECT_SOUND_PATH)
            self._spark_fx = self._load(self.SPARK_SOUND_PATH)

    @staticmethod
    def _load(rel_path: str):
        try:
            if not os.path.isfile(rel_path):
                return None
            fx = QSoundEffect()
            fx.setSource(QUrl.fromLocalFile(os.path.abspath(rel_path)))
            fx.setVolume(0.5)
            return fx
        except Exception:
            return None

    def play_disconnect(self):
        try:
            if self._disconnect_fx is not None:
                self._disconnect_fx.play()
        except Exception:
            pass

    def play_spark(self):
        try:
            if self._spark_fx is not None:
                self._spark_fx.play()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  Spark particle + effect
# ─────────────────────────────────────────────────────────────────────────────

class Particle:
    """A single tiny purple spark particle."""

    def __init__(self, cx: float, cy: float):
        angle = random.uniform(0, math.tau)
        speed = random.uniform(1.2, 3.6)          # subtle, not explosive
        self.x, self.y = cx, cy
        self.vx = math.cos(angle) * speed
        self.vy = math.sin(angle) * speed - 0.6    # slight upward bias
        self.life = 1.0
        self.decay = random.uniform(0.045, 0.09)   # disappear quickly
        self.size = random.uniform(1.0, 2.6)
        # Electric purple neon palette, matching #BF00FF
        self.color = QColor(
            random.randint(180, 210),
            random.randint(0, 60),
            255,
        )

    def update(self):
        self.x += self.vx
        self.y += self.vy
        self.vy += 0.05
        self.vx *= 0.94
        self.vy *= 0.94
        self.life -= self.decay

    @property
    def alive(self) -> bool:
        return self.life > 0

    def draw(self, p: QPainter):
        if not self.alive:
            return
        alpha = int(255 * self.life)
        c = QColor(self.color.red(), self.color.green(), self.color.blue(), alpha)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        r = self.size * self.life
        p.drawEllipse(QPointF(self.x, self.y), r, r)


class SparkEffect:
    """Owns and animates a small burst of Particles plus a brief flash."""

    def __init__(self):
        self._particles = []
        self._flash = 0.0     # 0..1, quick radial flash at the spark point
        self._origin = QPointF(0, 0)
        self._triggered = False

    def trigger(self, x: float, y: float):
        self._origin = QPointF(x, y)
        self._flash = 1.0
        self._triggered = True
        for _ in range(10):        # small handful of particles, not a burst
            self._particles.append(Particle(x, y))

    def update(self):
        if not self._triggered:
            return
        self._flash = max(0.0, self._flash - 0.14)
        for part in self._particles:
            part.update()
        self._particles = [pt for pt in self._particles if pt.alive]

    @property
    def is_active(self) -> bool:
        return self._flash > 0 or len(self._particles) > 0

    def draw(self, p: QPainter):
        if not self._triggered:
            return
        if self._flash > 0:
            r = 6 + 22 * (1 - self._flash)
            glow = QRadialGradient(self._origin, r)
            glow.setColorAt(0.0, QColor(230, 200, 255, int(230 * self._flash)))
            glow.setColorAt(0.5, QColor(191, 0, 255, int(140 * self._flash)))
            glow.setColorAt(1.0, QColor(191, 0, 255, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(glow))
            p.drawEllipse(self._origin, r, r)
        for part in self._particles:
            part.draw(p)



# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers for image-based hardware rendering
# ─────────────────────────────────────────────────────────────────────────────

NEON_COLOR = QColor(191, 0, 255)   # #BF00FF — electric purple





def _draw_body_glow(p: QPainter, center: QPointF, radius: float, alpha_mult: float):
    c = NEON_COLOR
    glow = QRadialGradient(center, radius)
    glow.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(), int(170 * alpha_mult)))
    glow.setColorAt(0.55, QColor(c.red(), c.green(), c.blue(), int(80 * alpha_mult)))
    glow.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))
    p.setBrush(QBrush(glow))
    p.setPen(Qt.NoPen)
    p.drawEllipse(center, radius, radius)


class ConnectedCapsule:
    """
    The single merged connector body shown while plug + socket are still
    "plugged in" (reference image 1). Rendered entirely from the real
    reference artwork (assets/unplug/connected.png) in three horizontal
    slices: the body (drawn at fixed, undistorted scale) plus a cable
    slice on each side that is *stretched* to reach the screen edges —
    no procedural cable is drawn at all; it's images only, all the way
    to both edges of the window, however wide it is.

    Drawn only during HOLD and the very start of the disconnect (it
    crossfades into the separate Plug/Socket halves via
    AnimationController.morph_progress).
    """

    FILENAME = 'connected.png'

    # Anchor points measured directly from the source image (pixel space)
    ANCHOR_Y = 75
    BODY_LEFT_X = 486
    BODY_RIGHT_X = 723
    CONTENT_LEFT_X = 0
    CONTENT_RIGHT_X = 1223
    BODY_HEIGHT_PX = 93      # tallest point of the capsule body, for scaling
    DISPLAY_HEIGHT = 108     # desired on-screen body height

    GLOW_RADIUS = 150

    def __init__(self):
        self.glow_alpha = 1.0
        self.breathe = 1.0
        self._pixmap = _load_pixmap(self.FILENAME)
        self._scale = self.DISPLAY_HEIGHT / self.BODY_HEIGHT_PX

    def draw(self, p: QPainter, cx: float, cy: float, screen_w: float):
        if self._pixmap.isNull():
            return
        p.save()
        a = self.glow_alpha
        pulse = self.breathe
        s = self._scale
        img_h = self._pixmap.height()
        top = cy - self.ANCHOR_Y * s
        h_screen = img_h * s

        body_w_screen = (self.BODY_RIGHT_X - self.BODY_LEFT_X) * s
        body_left = cx - body_w_screen / 2
        body_right = cx + body_w_screen / 2

        _draw_body_glow(p, QPointF(cx, cy), self.GLOW_RADIUS, a * pulse)

        p.setOpacity(a)
        if body_left > 0:
            src = QRectF(self.CONTENT_LEFT_X, 0, self.BODY_LEFT_X - self.CONTENT_LEFT_X, img_h)
            dest = QRectF(0, top, body_left, h_screen)
            p.drawPixmap(dest, self._pixmap, src)

        src_body = QRectF(self.BODY_LEFT_X, 0, self.BODY_RIGHT_X - self.BODY_LEFT_X, img_h)
        dest_body = QRectF(body_left, top, body_w_screen, h_screen)
        p.drawPixmap(dest_body, self._pixmap, src_body)

        if screen_w > body_right:
            src = QRectF(self.BODY_RIGHT_X, 0, self.CONTENT_RIGHT_X - self.BODY_RIGHT_X, img_h)
            dest = QRectF(body_right, top, screen_w - body_right, h_screen)
            p.drawPixmap(dest, self._pixmap, src)
        p.restore()


# ─────────────────────────────────────────────────────────────────────────────
#  Plug (left half) and Socket (right half)
# ─────────────────────────────────────────────────────────────────────────────

class Plug:
    """
    Left-hand connector body: real reference artwork (assets/unplug/plug.png),
    drawn as a fixed-scale body plus a cable slice stretched from the
    body's back edge all the way to the left screen edge.
    """

    FILENAME = 'plug.png'

    ANCHOR_Y = 75
    BODY_LEFT_X = 408       # where the cable meets the body
    CONTENT_RIGHT_X = 528   # closed tip (right edge) — the gap boundary
    CONTENT_LEFT_X = 0
    BODY_HEIGHT_PX = 95
    DISPLAY_HEIGHT = 108

    GLOW_RADIUS = 115

    def __init__(self):
        self.glow_alpha = 1.0
        self.breathe = 1.0
        self._pixmap = _load_pixmap(self.FILENAME)
        self._scale = self.DISPLAY_HEIGHT / self.BODY_HEIGHT_PX

    def tip_point(self, tip_x: float, cy: float) -> QPointF:
        return QPointF(tip_x, cy)

    def draw(self, p: QPainter, tip_x: float, cy: float, screen_left: float = 0.0):
        if self._pixmap.isNull():
            return
        p.save()
        a = self.glow_alpha
        pulse = self.breathe
        s = self._scale
        img_h = self._pixmap.height()
        top = cy - self.ANCHOR_Y * s
        h_screen = img_h * s

        body_w_screen = (self.CONTENT_RIGHT_X - self.BODY_LEFT_X) * s
        body_left = tip_x - body_w_screen

        _draw_body_glow(p, QPointF(tip_x - 30 * s, cy), self.GLOW_RADIUS, a * pulse)

        p.setOpacity(a)
        if body_left > screen_left:
            src = QRectF(self.CONTENT_LEFT_X, 0, self.BODY_LEFT_X - self.CONTENT_LEFT_X, img_h)
            dest = QRectF(screen_left, top, body_left - screen_left, h_screen)
            p.drawPixmap(dest, self._pixmap, src)

        src_body = QRectF(self.BODY_LEFT_X, 0, self.CONTENT_RIGHT_X - self.BODY_LEFT_X, img_h)
        dest_body = QRectF(body_left, top, body_w_screen, h_screen)
        p.drawPixmap(dest_body, self._pixmap, src_body)
        p.restore()


class Socket:
    """
    Right-hand connector body: real reference artwork
    (assets/unplug/socket.png), drawn as a fixed-scale body plus a cable
    slice stretched from the body's back edge all the way to the right
    screen edge.
    """

    FILENAME = 'socket.png'

    ANCHOR_Y = 75
    CONTENT_LEFT_X = 57     # prong tips — the gap boundary
    BODY_RIGHT_X = 227      # where the body meets the cable
    CONTENT_RIGHT_X = 636
    BODY_HEIGHT_PX = 95
    DISPLAY_HEIGHT = 108

    GLOW_RADIUS = 115

    def __init__(self):
        self.glow_alpha = 1.0
        self.breathe = 1.0
        self._pixmap = _load_pixmap(self.FILENAME)
        self._scale = self.DISPLAY_HEIGHT / self.BODY_HEIGHT_PX

    def tip_point(self, tip_x: float, cy: float) -> QPointF:
        return QPointF(tip_x, cy)

    def draw(self, p: QPainter, tip_x: float, cy: float, screen_right: float = None):
        if self._pixmap.isNull():
            return
        p.save()
        a = self.glow_alpha
        pulse = self.breathe
        s = self._scale
        img_h = self._pixmap.height()
        top = cy - self.ANCHOR_Y * s
        h_screen = img_h * s

        body_w_screen = (self.BODY_RIGHT_X - self.CONTENT_LEFT_X) * s
        body_right = tip_x + body_w_screen
        if screen_right is None:
            screen_right = body_right

        _draw_body_glow(p, QPointF(tip_x + 30 * s, cy), self.GLOW_RADIUS, a * pulse)

        p.setOpacity(a)
        src_body = QRectF(self.CONTENT_LEFT_X, 0, self.BODY_RIGHT_X - self.CONTENT_LEFT_X, img_h)
        dest_body = QRectF(tip_x, top, body_w_screen, h_screen)
        p.drawPixmap(dest_body, self._pixmap, src_body)

        if screen_right > body_right:
            src = QRectF(self.BODY_RIGHT_X, 0, self.CONTENT_RIGHT_X - self.BODY_RIGHT_X, img_h)
            dest = QRectF(body_right, top, screen_right - body_right, h_screen)
            p.drawPixmap(dest, self._pixmap, src)
        p.restore()


# ─────────────────────────────────────────────────────────────────────────────
#  UnplugText — "UN" (travels with Plug) + "PLUG" (travels with Socket)
# ─────────────────────────────────────────────────────────────────────────────

class UnplugText:
    """
    Renders "UN" / "PLUG" as hollow neon-tube outline letters — stroke
    only, transparent interior — matching the reference image's "double
    lined" wordmark rather than solid filled letters.
    """

    LEFT_PART = 'UN'
    RIGHT_PART = 'PLUG'

    def __init__(self):
        self.glow_alpha = 1.0
        self.breathe = 1.0
        self._font = QFont('Arial', 68, QFont.Black)
        self._font.setLetterSpacing(QFont.PercentageSpacing, 145)

    def _glyph_path(self, text: str, x: float, baseline_y: float) -> QPainterPath:
        path = QPainterPath()
        path.addText(x, baseline_y, self._font, text)
        return path

    def draw(self, p: QPainter, cx: float, text_y: float, gap: float):
        """
        cx: horizontal centre of the screen — "UN"+"PLUG" sit perfectly
            adjacent here at gap=0, reading as one evenly-spaced word.
        gap: 0 at rest, growing during the disconnect phase. "UN" drifts
             left by `gap`, "PLUG" drifts right by `gap`, moving in sync
             with the plug/socket so the text stays visually attached to
             the hardware as it separates.
        """
        p.save()
        p.setFont(self._font)
        fm = p.fontMetrics()
        un_w = fm.horizontalAdvance(self.LEFT_PART)
        baseline_y = text_y + fm.ascent() / 2

        a = self.glow_alpha
        pulse = self.breathe
        core = QColor(255, 255, 255, int(255 * a))
        halo = QColor(191, 0, 255, int(255 * a * pulse))

        # "UN" ends exactly at the seam (right-aligned), "PLUG" starts
        # exactly at the seam (left-aligned) — at gap=0 they touch with
        # only the font's own letter-spacing between them, so all letters
        # in "UNPLUG" read as evenly spaced. As `gap` grows they peel
        # apart symmetrically, each half following its own hardware.
        un_x = cx - un_w - gap
        plug_x_start = cx + gap

        un_path = self._glyph_path(self.LEFT_PART, un_x, baseline_y)
        plug_path = self._glyph_path(self.RIGHT_PART, plug_x_start, baseline_y)

        p.setBrush(Qt.NoBrush)
        for path in (un_path, plug_path):
            # Soft outer glow layers (wide, low alpha) — brighter per feedback
            for width, alpha_mult in ((14, 0.12), (9, 0.22), (5, 0.36)):
                p.setPen(QPen(QColor(halo.red(), halo.green(), halo.blue(),
                                      int(halo.alpha() * alpha_mult)),
                               width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                p.drawPath(path)
            # Crisp bright outline — this is the "double line" edge of each
            # letterform (inner + outer edge of the stroke), hollow inside.
            p.setPen(QPen(core, 2.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
        p.restore()


# ─────────────────────────────────────────────────────────────────────────────
#  AnimationController — pure timing/phase math, no painting
# ─────────────────────────────────────────────────────────────────────────────

class AnimationController:
    """
    Drives the phase state machine and produces all derived animation
    values (positions, alphas, easing) for a given elapsed time. Keeps
    timing/physics logic separate from drawing code.
    """

    HOLD = 0
    DISCONNECT = 1
    FADE = 2
    DONE = 3

    DUR_HOLD = 1600
    DUR_DISCONNECT = 3400
    DUR_FADE = 2000
    TOTAL_MS = DUR_HOLD + DUR_DISCONNECT + DUR_FADE   # ≈ 7s

    MAX_SEPARATION = 0.20   # fraction of screen width each half travels
    MORPH_MS = 260          # crossfade: merged capsule → separate plug/socket

    def __init__(self):
        self.elapsed_ms = 0
        self.phase = self.HOLD
        self.spark_fired = False
        self._t_hold_end = self.DUR_HOLD
        self._t_disconnect_end = self.DUR_HOLD + self.DUR_DISCONNECT
        self._t_fade_end = self.TOTAL_MS

    def advance(self, dt_ms: float):
        self.elapsed_ms += dt_ms
        if self.elapsed_ms < self._t_hold_end:
            self.phase = self.HOLD
        elif self.elapsed_ms < self._t_disconnect_end:
            self.phase = self.DISCONNECT
        elif self.elapsed_ms < self._t_fade_end:
            self.phase = self.FADE
        else:
            self.phase = self.DONE

    @property
    def finished(self) -> bool:
        return self.phase == self.DONE

    @property
    def just_reached_spark_moment(self) -> bool:
        """True exactly once, the tick the disconnect completes."""
        return (not self.spark_fired) and self.elapsed_ms >= self._t_disconnect_end

    # ── Derived values ──────────────────────────────────────────

    def separation_progress(self) -> float:
        """0..1 progress through the DISCONNECT phase (physical ease-out)."""
        if self.elapsed_ms < self._t_hold_end:
            return 0.0
        if self.elapsed_ms >= self._t_disconnect_end:
            return 1.0
        local_t = (self.elapsed_ms - self._t_hold_end) / self.DUR_DISCONNECT
        return ease_out_cubic(local_t)

    def breathing_pulse(self) -> float:
        """Slow ambient glow pulse, active through HOLD + DISCONNECT."""
        cycle = self.elapsed_ms / 1000.0
        return 0.82 + 0.18 * (0.5 + 0.5 * math.sin(cycle * 2.4))

    def morph_progress(self) -> float:
        """
        0..1 crossfade from the merged "connected" capsule (image-1 style)
        into the separate plug/socket halves (image-2 style). Runs for a
        brief window right as the DISCONNECT phase begins — 0 = fully
        merged capsule, 1 = fully separate hardware.
        """
        if self.elapsed_ms < self._t_hold_end:
            return 0.0
        local_t = (self.elapsed_ms - self._t_hold_end) / self.MORPH_MS
        return ease_in_out_cubic(min(1.0, max(0.0, local_t)))

    def fade_progress(self) -> float:
        """0..1 through the FADE phase."""
        if self.elapsed_ms < self._t_disconnect_end:
            return 0.0
        if self.elapsed_ms >= self._t_fade_end:
            return 1.0
        local_t = (self.elapsed_ms - self._t_disconnect_end) / self.DUR_FADE
        return ease_in_out_cubic(local_t)


# ─────────────────────────────────────────────────────────────────────────────
#  OpeningAnimation — top-level widget
# ─────────────────────────────────────────────────────────────────────────────

class OpeningAnimation(QWidget):
    """
    Full-screen cinematic opening for Unplug Mode.

    Ends on a solid black screen and emits `animation_done` — it does not
    transition anywhere further (see extension note at the bottom of this
    file for how a later phase should hook in).
    """

    animation_done = pyqtSignal()

    TICK_MS = 16   # ~60 FPS

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, False)

        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

        self._controller = AnimationController()
        self._capsule = ConnectedCapsule()
        self._plug = Plug()
        self._socket = Socket()
        self._text = UnplugText()
        self._spark = SparkEffect()
        self._sound = _SoundHooks()

        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._on_tick)

    # ── Public API ──────────────────────────────────────────────

    def start(self):
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)
        self._controller = AnimationController()
        self._spark = SparkEffect()
        self.show()
        self.raise_()
        self._tick_timer.start(self.TICK_MS)

    # ── Tick / lifecycle ────────────────────────────────────────

    def _on_tick(self):
        self._controller.advance(self.TICK_MS)

        if self._controller.just_reached_spark_moment:
            self._controller.spark_fired = True
            self._fire_spark()

        self._spark.update()

        if self._controller.finished and not self._spark.is_active:
            self._finish()
            return

        self.update()

    def _fire_spark(self):
        w, h = self.width(), self.height()
        cx = w / 2.0
        hw_y = h / 2.0 + h * 0.16
        self._spark.trigger(cx, hw_y)
        self._sound.play_disconnect()
        self._sound.play_spark()

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

        ctrl = self._controller
        sep_progress = ctrl.separation_progress()
        morph = ctrl.morph_progress()
        pulse = ctrl.breathing_pulse()
        fade = ctrl.fade_progress()

        # ── Background ──────────────────────────────────────────
        bg = QLinearGradient(0, 0, 0, h)
        bg.setColorAt(0.0, QColor(6, 4, 12))
        bg.setColorAt(1.0, QColor(2, 1, 6))
        p.fillRect(0, 0, w, h, QBrush(bg))

        # Faint vertical "brick wall" glow bands, echoing the reference art
        p.setOpacity(0.35)
        band_pen = QPen(QColor(40, 20, 70, 60), 1)
        p.setPen(band_pen)
        for x in range(0, w, 46):
            p.drawLine(x, 0, x, h)
        p.setOpacity(1.0)

        # ── Positions ────────────────────────────────────────────
        # Plug's tip and Socket's tip sit at cx∓gap — they touch exactly
        # at gap=0 (the "plugged in" resting state) and separate
        # symmetrically as `gap` grows.
        max_sep_px = w * ctrl.MAX_SEPARATION
        gap = sep_progress * max_sep_px

        plug_tip_x = cx - gap
        socket_tip_x = cx + gap
        hw_y = cy + h * 0.16          # hardware row, below the text
        text_y = cy - h * 0.10        # wordmark row, above the hardware

        # Alphas fade together during FADE phase. Plug/Socket also fade IN
        # via `morph` (0 = fully merged capsule, 1 = fully separate
        # hardware); the capsule fades out over the same window.
        alpha_mult = 1.0 - fade

        self._plug.glow_alpha = alpha_mult * morph
        self._plug.breathe = pulse
        self._socket.glow_alpha = alpha_mult * morph
        self._socket.breathe = pulse
        self._capsule.glow_alpha = alpha_mult * (1.0 - morph)
        self._capsule.breathe = pulse
        self._text.glow_alpha = alpha_mult
        self._text.breathe = pulse

        # ── Hardware: merged capsule (image 1) crossfades into the
        #    separate plug/socket (image 2) as `morph` advances. Each
        #    piece stretches its own baked-in cable to the screen edge —
        #    no separate procedural cable is drawn anywhere. ──────────
        if morph < 1.0:
            self._capsule.draw(p, cx, hw_y, w)
        if morph > 0.0:
            self._plug.draw(p, plug_tip_x, hw_y, 0.0)
            self._socket.draw(p, socket_tip_x, hw_y, w)

        # ── Text ───────────────────────────────────────────────
        self._text.draw(p, cx, text_y, gap)

        # ── Spark (always on top) ───────────────────────────────
        self._spark.draw(p)

        # ── Final fade-to-black overlay ─────────────────────────
        if fade > 0:
            p.fillRect(0, 0, w, h, QColor(0, 0, 0, int(255 * fade)))


# ─────────────────────────────────────────────────────────────────────────────
#  What follows this animation
# ─────────────────────────────────────────────────────────────────────────────
#
# This phase stops on a black screen and emits `animation_done`. That's
# now wired in ui/tray_app.py's trigger_unplug_mode(): the callback
# creates and shows ExerciseEnvironment (ui/unplug/exercise_environment.py
# — the Three.js wellness scene) while the screen is still black, so the
# cut feels seamless rather than flashing back to the desktop first.
#
#     anim = OpeningAnimation()
#     anim.animation_done.connect(lambda: ExerciseEnvironment(character=...).start())
#     anim.start()
#
# ─────────────────────────────────────────────────────────────────────────────


if __name__ == '__main__':
    app = QApplication(sys.argv)
    anim = OpeningAnimation()
    anim.animation_done.connect(lambda: (print('Opening animation done — black screen reached'), app.quit()))
    anim.start()
    sys.exit(app.exec_())
