"""
calm_mode.py — Canary Calm Mode (Phase 1)
==========================================
Short visual breathing intervention over a flowing blue-wave backdrop.

The core idea: the particle FIELD ITSELF quiets down as the user
breathes. Each of the three breathing cycles ends with a portion of
the particles softly fading away, so the field goes from rich and
busy to almost empty. After the final exhale, exactly one particle
remains, settles at the center, and — using particles reactivated
from the same pool — builds a small circle, then two eyes, then a
smile, one particle at a time. The completed face holds briefly, then
screen quickly goes black and Calm Mode is gone.

    MANY → breathe → FEWER → breathe → FEWER STILL → breathe → ONE
    → circle (particle by particle) → eyes (one, then the other)
    → smile (particle by particle) → hold → drift apart → gone

Manual-only in this phase — no camera, no detector, no automatic
trigger. Architecture mirrors ui/horizon/horizon_mode.py: a frameless
always-on-top full-screen QWidget, a `finished` signal, a public
`start()`, and a single ~60fps QTimer driving a QPainter paintEvent
over a persistent, fixed-size list of particle objects — nothing is
allocated mid-animation. Particles retired from the breathing field
are not deleted; they are simply marked inactive and later reused
(reactivated) to build the circle/eyes/smile, so the whole ending
sequence costs zero new objects.

Usage
-----
    mode = CalmMode()
    mode.finished.connect(on_calm_done)
    mode.start()
"""

import math
import random
import sys

from PyQt5.QtWidgets import QWidget, QApplication
from PyQt5.QtCore import Qt, QTimer, QRectF, QPointF, pyqtSignal
from PyQt5.QtGui import (
    QPainter, QPainterPath, QColor, QRadialGradient, QBrush, QFont,
)


# ══════════════════════════════════════════════════════════════════════════
#  Named constants — timing, particle counts, colors
# ══════════════════════════════════════════════════════════════════════════

FRAME_MS = 16  # ~60 FPS

INHALE_SEC = 4.0
HOLD_SEC = 4.0
EXHALE_SEC = 6.0

BREATHING_CYCLES = 3

# ── Particle-count reduction, one step per breathing cycle ─────────────────
# Index 0 = particles active during cycle 1 (the richest field), etc.
# After the LAST cycle's exhale, the count drops all the way to
# FINAL_PARTICLE_COUNT (a single remaining particle) rather than to
# CYCLE_ACTIVE_COUNTS[-1] + 1.
INITIAL_PARTICLE_COUNT = 150
CYCLE_ACTIVE_COUNTS = [INITIAL_PARTICLE_COUNT, 55, 18]
FINAL_PARTICLE_COUNT = 1

# Reduction happens gently at the tail end of each EXHALE: particles
# chosen to retire hold full opacity until DYING_FADE_START_FRAC of the
# way through the exhale, then ease down to zero by the time it ends.
DYING_FADE_START_FRAC = 0.45

# Each successive cycle is very slightly more restrained (slower orbital
# drift) than the last — subtle, not a dramatic speed change.
CYCLE_RESTRAINT_STEP = 0.15  # cycle N's angular speed is scaled by (1 - N*step)

# ── Particle size mix — mostly small, some medium, very few larger accents.
SIZE_SMALL_RANGE = (1.1, 1.8)
SIZE_MEDIUM_RANGE = (2.0, 2.8)
SIZE_ACCENT_RANGE = (3.2, 4.2)
_SIZE_TIERS = (
    (SIZE_SMALL_RANGE, 0.75),
    (SIZE_MEDIUM_RANGE, 0.20),
    (SIZE_ACCENT_RANGE, 0.05),
)

# ── Ending sequence: how many pooled (retired) particles get reactivated
# to build the circle / eyes / smile. All drawn from the same
# INITIAL_PARTICLE_COUNT pool — nothing new is created.
CIRCLE_PARTICLE_COUNT = 50
EYE_PARTICLE_COUNT = 2
MOUTH_PARTICLE_COUNT = 26

EYE_SIZE = 8.5  # noticeably bigger than any regular field particle, not cartoonish

SETTLE_MOVE_SEC = 1.0     # the one surviving particle eases in to dead-center
SETTLE_PAUSE_SEC = 0.6    # a small pause — stillness before anything is built

CIRCLE_FORM_SEC = 3.6
CIRCLE_REVEAL_SPAN_FRAC = 0.8     # fraction of CIRCLE_FORM_SEC spent revealing dots
CIRCLE_PARTICLE_REVEAL_SEC = 0.25  # one dot's own pop-in duration
CIRCLE_RADIUS_FRAC = 1.0          # circle radius, relative to the breathing max radius —
                                   # scaled to match the size marked in the reference image

EYES_FORM_SEC = 1.0
EYE_GAP_SEC = 0.35     # pause between the left eye starting and the right eye starting
EYE_REVEAL_SEC = 0.35  # one eye's own fade/scale-in duration
EYE_OFFSET_X_FRAC = 0.44  # half eye-to-eye spacing, relative to circle radius
EYE_Y_FRAC = 0.43         # eyes sit above center by this much of the circle radius

MOUTH_FORM_SEC = 3.0
MOUTH_REVEAL_SPAN_FRAC = 0.75
MOUTH_PARTICLE_REVEAL_SEC = 0.22
MOUTH_WIDTH_FRAC = 0.92    # relative to circle radius
MOUTH_DEPTH_FRAC = 0.20
MOUTH_Y_FRAC = 0.33

SMILE_HOLD_SEC = 1.8

BLACKOUT_SEC = 0.5  # quick: screen goes black, then Calm Mode is gone

# ── Background: layered horizontal wave bands — unchanged, kept as-is.
BG_WAVE_COLORS = [
    QColor(0xC7, 0xE2, 0xF2),
    QColor(0x9F, 0xC7, 0xE6),
    QColor(0x72, 0xA6, 0xD6),
    QColor(0x49, 0x83, 0xBE),
    QColor(0x28, 0x5B, 0x92),
    QColor(0x10, 0x28, 0x40),
]
BG_WAVE_BASE_FRACS = [0.04, 0.20, 0.38, 0.56, 0.74, 0.90]
BG_WAVE_SAMPLES = 32
COLOR_BLACK = QColor(0x00, 0x00, 0x00)

# ── Particle palette — ocean/sky blues. Brighter blues dominate for
# visibility; darker blues appear more sparingly.
COLOR_MIDNIGHT = QColor(0x0A, 0x1A, 0x33)     # near-black navy
COLOR_NAVY = QColor(0x10, 0x24, 0x4A)
COLOR_DEEP_COBALT = QColor(0x16, 0x2F, 0x63)
COLOR_DARK_SAPPHIRE = QColor(0x1B, 0x3A, 0x7A)
COLOR_DARK_COBALT = QColor(0x20, 0x45, 0x94)  # the "lightest" shade in use — still dark
COLOR_TEXT = QColor(0xE8, 0xF6, 0xFF)

# Every particle — field, circle, eyes, and smile alike — draws from this
# same very-dark-blue set. No bright accents; the deepest shades dominate.
_PARTICLE_COLOR_BANDS = (
    ([COLOR_MIDNIGHT, COLOR_NAVY], 0.45),
    ([COLOR_DEEP_COBALT], 0.30),
    ([COLOR_DARK_SAPPHIRE], 0.18),
    ([COLOR_DARK_COBALT], 0.07),  # rare, only slightly lighter — still dark
)

# Soft glow/bloom drawn behind every particle so it stays visible without
# looking neon: a larger, faint halo behind the small solid core.
GLOW_SIZE_MULT = 2.2
GLOW_ALPHA_MULT = 0.30


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(_lerp(c1.red(), c2.red(), t)),
        int(_lerp(c1.green(), c2.green(), t)),
        int(_lerp(c1.blue(), c2.blue(), t)),
    )


def _ease_in_out(t: float) -> float:
    """Smooth cubic ease-in-out, t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return 4 * t * t * t
    p = -2 * t + 2
    return 1 - (p ** 3) / 2


def _pick_band_color() -> QColor:
    r = random.random()
    cum = 0.0
    for colors, weight in _PARTICLE_COLOR_BANDS:
        cum += weight
        if r <= cum:
            return random.choice(colors)
    return COLOR_NAVY


def _pick_size() -> float:
    r = random.random()
    cum = 0.0
    for size_range, weight in _SIZE_TIERS:
        cum += weight
        if r <= cum:
            return random.uniform(*size_range)
    return random.uniform(*SIZE_SMALL_RANGE)


# ══════════════════════════════════════════════════════════════════════════
#  Particle
# ══════════════════════════════════════════════════════════════════════════

class CalmParticle:
    """
    One particle. Persistent object — reused throughout the mode's
    lifetime, never recreated. During the breathing field it either
    stays `active` or is gently retired (`active = False`). Retired
    particles are the pool the ending sequence reactivates to build
    the circle, eyes, and smile — so nothing new is ever allocated.
    """

    def __init__(self):
        self.base_angle = random.uniform(0, 2 * math.pi)
        self.angular_speed = random.uniform(0.05, 0.14) * random.choice((-1, 1))
        self.radius_factor = random.uniform(0.82, 1.18)
        self.size = _pick_size()
        self.opacity_factor = random.uniform(0.55, 1.0)
        self.color = _pick_band_color()

        self.active = True     # currently part of the visible field/face
        self.role = 'field'    # 'field' | 'circle' | 'eye' | 'mouth'

        # Retirement fade-out (breathing field only)
        self.dying = False
        self.fade_mult = 1.0

        # Reveal-in state (circle/eye/mouth roles during the ending sequence)
        self.target = None
        self.reveal_start_t = 0.0
        self.reveal_alpha = 1.0
        self.reveal_scale = 1.0

        # Scratch position used only while the lone survivor eases to center
        self.settle_origin = None

        self.angle = self.base_angle
        self.x = 0.0
        self.y = 0.0

    def update_orbit(self, dt: float):
        self.angle += self.angular_speed * dt


# ══════════════════════════════════════════════════════════════════════════
#  Main widget
# ══════════════════════════════════════════════════════════════════════════

class CalmMode(QWidget):
    """
    Full-screen breathing intervention over a flowing blue-wave backdrop.

    Phase sequence:
        intro → (inhale → hold → exhale) × BREATHING_CYCLES
              → settle_single → settle_pause
              → circle_form → eyes_form → mouth_form → smile_hold
              → blackout → finished
    """

    finished = pyqtSignal()

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

        self._particles: list = []
        self._wave_bands: list = []

        self._phase = 'intro'
        self._phase_t = 0.0
        self._elapsed_t = 0.0
        self._cycle_index = 0
        self._intro_alpha = 0.0
        self._bg_darken = 0.0
        self._min_radius_frac = 0.12

        self._dying_particles: list = []
        self._dying_assigned_this_exhale = False

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick)

    # ── Public ──────────────────────────────────────────────────────────

    def start(self):
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

        self._build_wave_bands()

        self._particles = [CalmParticle() for _ in range(INITIAL_PARTICLE_COUNT)]

        self._phase = 'intro'
        self._phase_t = 0.0
        self._elapsed_t = 0.0
        self._cycle_index = 0
        self._intro_alpha = 0.0
        self._bg_darken = 0.0
        self._dying_particles = []
        self._dying_assigned_this_exhale = False

        self.show()
        self.raise_()
        self.activateWindow()

        self._anim_timer.start(FRAME_MS)

    # ── Geometry helpers ────────────────────────────────────────────────

    def _breathing_max_radius(self) -> float:
        return min(self.width(), self.height()) * 0.28

    def _center(self) -> QPointF:
        return QPointF(self.width() / 2.0, self.height() / 2.0)

    def _build_wave_bands(self):
        h = self.height()
        self._wave_bands = []
        for base_frac, color in zip(BG_WAVE_BASE_FRACS, BG_WAVE_COLORS):
            self._wave_bands.append({
                'base_frac': base_frac,
                'color': color,
                'amp1': random.uniform(0.015, 0.032) * h,
                'amp2': random.uniform(0.008, 0.018) * h,
                'freq1': random.uniform(0.5, 1.1),
                'freq2': random.uniform(1.2, 2.1),
                'phase1': random.uniform(0, 2 * math.pi),
                'phase2': random.uniform(0, 2 * math.pi),
                'drift_speed': random.uniform(0.02, 0.05) * random.choice((-1, 1)),
            })

    # ── Breathing amount ─────────────────────────────────────────────────

    def _breath_amount(self) -> float:
        if self._phase == 'inhale':
            return _ease_in_out(min(1.0, self._phase_t / INHALE_SEC))
        if self._phase == 'hold':
            return 1.0
        if self._phase == 'exhale':
            t = min(1.0, self._phase_t / EXHALE_SEC)
            return 1.0 - _ease_in_out(t)
        return 0.0

    def _cycle_restraint(self) -> float:
        """Each cycle drifts very slightly slower than the last."""
        return max(0.4, 1.0 - self._cycle_index * CYCLE_RESTRAINT_STEP)

    # ── Main tick ───────────────────────────────────────────────────────

    def _tick(self):
        dt = FRAME_MS / 1000.0
        self._phase_t += dt
        self._elapsed_t += dt

        if self._intro_alpha < 1.0:
            self._intro_alpha = min(1.0, self._intro_alpha + 0.02)

        if self._phase in ('intro', 'inhale', 'hold', 'exhale'):
            restraint = self._cycle_restraint()
            for p in self._particles:
                if p.active:
                    p.update_orbit(dt * restraint)

        if self._phase == 'intro':
            if self._phase_t >= 0.8:
                self._enter_phase('inhale')

        elif self._phase == 'inhale':
            if self._phase_t >= INHALE_SEC:
                self._enter_phase('hold')

        elif self._phase == 'hold':
            if self._phase_t >= HOLD_SEC:
                self._enter_phase('exhale')

        elif self._phase == 'exhale':
            if not self._dying_assigned_this_exhale:
                self._begin_exhale_reduction()
                self._dying_assigned_this_exhale = True
            self._update_dying_fade()

            if self._phase_t >= EXHALE_SEC:
                self._finalize_exhale_reduction()
                self._cycle_index += 1
                if self._cycle_index >= BREATHING_CYCLES:
                    self._enter_phase('settle_single')
                else:
                    self._dying_assigned_this_exhale = False
                    self._enter_phase('inhale')

        elif self._phase == 'settle_single':
            if self._phase_t >= SETTLE_MOVE_SEC:
                self._enter_phase('settle_pause')

        elif self._phase == 'settle_pause':
            if self._phase_t >= SETTLE_PAUSE_SEC:
                self._begin_circle_form()
                self._enter_phase('circle_form')

        elif self._phase == 'circle_form':
            if self._phase_t >= CIRCLE_FORM_SEC:
                self._begin_eyes_form()
                self._enter_phase('eyes_form')

        elif self._phase == 'eyes_form':
            if self._phase_t >= EYES_FORM_SEC:
                self._begin_mouth_form()
                self._enter_phase('mouth_form')

        elif self._phase == 'mouth_form':
            if self._phase_t >= MOUTH_FORM_SEC:
                self._enter_phase('smile_hold')

        elif self._phase == 'smile_hold':
            if self._phase_t >= SMILE_HOLD_SEC:
                self._enter_phase('blackout')

        elif self._phase == 'blackout':
            self._bg_darken = min(1.0, self._phase_t / BLACKOUT_SEC)
            if self._phase_t >= BLACKOUT_SEC:
                self._finish()
                return

        self._update_particle_positions()
        self.update()

    def _enter_phase(self, phase: str):
        self._phase = phase
        self._phase_t = 0.0

    # ── Breathing-field particle reduction ───────────────────────────────

    def _begin_exhale_reduction(self):
        """
        Decide which particles retire at the end of THIS exhale, and
        mark them dying. Indices [0, next_count) always keep surviving;
        indices [next_count, current_count) are this exhale's casualties.
        """
        current_count = CYCLE_ACTIVE_COUNTS[self._cycle_index]
        if self._cycle_index == BREATHING_CYCLES - 1:
            next_count = FINAL_PARTICLE_COUNT
        else:
            next_count = CYCLE_ACTIVE_COUNTS[self._cycle_index + 1]

        self._dying_particles = self._particles[next_count:current_count]
        for p in self._dying_particles:
            p.dying = True
            p.fade_mult = 1.0

    def _update_dying_fade(self):
        fade_start_t = EXHALE_SEC * DYING_FADE_START_FRAC
        span = EXHALE_SEC - fade_start_t
        for p in self._dying_particles:
            if self._phase_t < fade_start_t:
                p.fade_mult = 1.0
            else:
                p.fade_mult = max(0.0, 1.0 - (self._phase_t - fade_start_t) / span)

    def _finalize_exhale_reduction(self):
        for p in self._dying_particles:
            p.active = False
            p.dying = False
            p.fade_mult = 1.0
        self._dying_particles = []

    # ── Ending sequence setup ────────────────────────────────────────────

    def _reuse_pool(self, count: int) -> list:
        """Pull `count` currently-inactive particles to reactivate."""
        pool = [p for p in self._particles if not p.active]
        return pool[:count]

    def _begin_circle_form(self):
        center = self._center()
        max_r = self._breathing_max_radius()
        radius = max_r * CIRCLE_RADIUS_FRAC

        circle_particles = self._reuse_pool(CIRCLE_PARTICLE_COUNT)
        n = len(circle_particles)
        reveal_span = CIRCLE_FORM_SEC * CIRCLE_REVEAL_SPAN_FRAC
        for i, p in enumerate(circle_particles):
            ang = (i / max(1, n)) * 2 * math.pi
            tx = center.x() + radius * math.cos(ang)
            ty = center.y() + radius * math.sin(ang)
            p.active = True
            p.role = 'circle'
            p.target = (tx, ty)
            p.reveal_start_t = (i / max(1, n - 1)) * reveal_span
            p.reveal_alpha = 0.0
            p.x, p.y = center.x(), center.y()
        self._circle_particles = circle_particles

    def _begin_eyes_form(self):
        center = self._center()
        max_r = self._breathing_max_radius()
        radius = max_r * CIRCLE_RADIUS_FRAC

        eye_particles = self._reuse_pool(EYE_PARTICLE_COUNT)
        eye_offset_x = radius * EYE_OFFSET_X_FRAC
        eye_y = center.y() - radius * EYE_Y_FRAC
        for i, p in enumerate(eye_particles):
            x = center.x() + (eye_offset_x if i == 1 else -eye_offset_x)
            p.active = True
            p.role = 'eye'
            p.size = EYE_SIZE
            p.target = (x, eye_y)
            p.reveal_start_t = 0.0 if i == 0 else EYE_GAP_SEC
            p.reveal_alpha = 0.0
            p.reveal_scale = 0.0
            p.x, p.y = center.x(), center.y()
        self._eye_particles = eye_particles

    def _begin_mouth_form(self):
        center = self._center()
        max_r = self._breathing_max_radius()
        radius = max_r * CIRCLE_RADIUS_FRAC

        mouth_particles = self._reuse_pool(MOUTH_PARTICLE_COUNT)
        mouth_width = radius * MOUTH_WIDTH_FRAC
        mouth_depth = radius * MOUTH_DEPTH_FRAC
        mouth_base_y = center.y() + radius * MOUTH_Y_FRAC

        n = len(mouth_particles)
        reveal_span = MOUTH_FORM_SEC * MOUTH_REVEAL_SPAN_FRAC
        for i, p in enumerate(mouth_particles):
            t = i / max(1, n - 1)
            x = center.x() + (t - 0.5) * mouth_width
            y = mouth_base_y + mouth_depth * (1 - (2 * t - 1) ** 2)
            jitter = random.uniform(-2.0, 2.0)
            p.active = True
            p.role = 'mouth'
            p.target = (x, y + jitter)
            p.reveal_start_t = t * reveal_span
            p.reveal_alpha = 0.0
            p.x, p.y = center.x(), center.y()
        self._mouth_particles = mouth_particles

    # ── Particle placement ─────────────────────────────────────────────

    def _update_particle_positions(self):
        center = self._center()
        max_r = self._breathing_max_radius()

        if self._phase in ('intro', 'inhale', 'hold', 'exhale'):
            amount = self._breath_amount()
            radius_frac = self._min_radius_frac + (1.0 - self._min_radius_frac) * amount
            for p in self._particles:
                if not p.active:
                    continue
                r = max_r * radius_frac * p.radius_factor
                p.x = center.x() + r * math.cos(p.angle)
                p.y = center.y() + r * math.sin(p.angle)

        elif self._phase == 'settle_single':
            survivor = self._particles[0]
            t = _ease_in_out(min(1.0, self._phase_t / SETTLE_MOVE_SEC))
            # capture the start position once, on the first frame of this phase
            if self._phase_t <= FRAME_MS / 1000.0:
                survivor.settle_origin = (survivor.x, survivor.y)
            sx, sy = survivor.settle_origin or (survivor.x, survivor.y)
            survivor.x = _lerp(sx, center.x(), t)
            survivor.y = _lerp(sy, center.y(), t)

        elif self._phase == 'settle_pause':
            survivor = self._particles[0]
            survivor.x, survivor.y = center.x(), center.y()

        elif self._phase == 'circle_form':
            self._particles[0].x, self._particles[0].y = center.x(), center.y()
            for p in getattr(self, '_circle_particles', []):
                self._advance_reveal(p, center, CIRCLE_PARTICLE_REVEAL_SEC)

        elif self._phase == 'eyes_form':
            self._particles[0].x, self._particles[0].y = center.x(), center.y()
            for p in getattr(self, '_circle_particles', []):
                p.x, p.y = p.target
                p.reveal_alpha = 1.0
            for p in getattr(self, '_eye_particles', []):
                self._advance_eye_reveal(p, center)

        elif self._phase == 'mouth_form':
            self._particles[0].x, self._particles[0].y = center.x(), center.y()
            for p in getattr(self, '_circle_particles', []):
                p.x, p.y = p.target
                p.reveal_alpha = 1.0
            for p in getattr(self, '_eye_particles', []):
                p.x, p.y = p.target
                p.reveal_alpha = 1.0
                p.reveal_scale = 1.0
            for p in getattr(self, '_mouth_particles', []):
                self._advance_reveal(p, center, MOUTH_PARTICLE_REVEAL_SEC)

        elif self._phase == 'smile_hold':
            self._particles[0].x, self._particles[0].y = center.x(), center.y()
            for group_name in ('_circle_particles', '_eye_particles', '_mouth_particles'):
                for p in getattr(self, group_name, []):
                    p.x, p.y = p.target
                    p.reveal_alpha = 1.0
                    p.reveal_scale = 1.0

        # 'blackout' needs no position updates — particles simply hold
        # their smile_hold positions while the screen fades to black.

    def _advance_reveal(self, p: 'CalmParticle', center: QPointF, reveal_sec: float):
        if self._phase_t < p.reveal_start_t:
            p.x, p.y = center.x(), center.y()
            p.reveal_alpha = 0.0
        else:
            prog = (self._phase_t - p.reveal_start_t) / reveal_sec
            eased = _ease_in_out(min(1.0, max(0.0, prog)))
            tx, ty = p.target
            p.x = _lerp(center.x(), tx, eased)
            p.y = _lerp(center.y(), ty, eased)
            p.reveal_alpha = eased

    def _advance_eye_reveal(self, p: 'CalmParticle', center: QPointF):
        if self._phase_t < p.reveal_start_t:
            p.x, p.y = center.x(), center.y()
            p.reveal_alpha = 0.0
            p.reveal_scale = 0.0
        else:
            prog = (self._phase_t - p.reveal_start_t) / EYE_REVEAL_SEC
            eased = _ease_in_out(min(1.0, max(0.0, prog)))
            tx, ty = p.target
            p.x = _lerp(center.x(), tx, eased)
            p.y = _lerp(center.y(), ty, eased)
            p.reveal_alpha = eased
            p.reveal_scale = eased  # soft scale-in, from nothing up to full size

    # ── Finish ──────────────────────────────────────────────────────────

    def _finish(self):
        self._anim_timer.stop()
        self.hide()
        self.finished.emit()

    # ── Paint ───────────────────────────────────────────────────────────

    def _draw_wave_background(self, painter: QPainter, w: int, h: int):
        # The topmost wave band's curve only covers the area BELOW it —
        # the strip above that curve (near y=0) was never painted at all,
        # so it fell back to the widget's plain background and never
        # darkened during blackout. Pre-fill the whole canvas with that
        # band's own (correctly darkened) color first so there's no gap.
        if self._wave_bands:
            top_color = self._wave_bands[0]['color']
            if self._bg_darken > 0:
                top_color = _lerp_color(top_color, COLOR_BLACK, self._bg_darken)
            painter.fillRect(0, 0, w, h, top_color)

        t = self._elapsed_t
        for band in self._wave_bands:
            path = QPainterPath()
            base_y = band['base_frac'] * h
            amp1, amp2 = band['amp1'], band['amp2']
            freq1, freq2 = band['freq1'], band['freq2']
            phase1, phase2 = band['phase1'], band['phase2']
            drift = t * band['drift_speed']

            first_y = base_y + amp1 * math.sin(phase1 + drift) + \
                amp2 * math.sin(phase2 - drift * 0.6)
            path.moveTo(0, first_y)
            for i in range(1, BG_WAVE_SAMPLES + 1):
                xf = i / BG_WAVE_SAMPLES
                x = xf * w
                y = base_y + \
                    amp1 * math.sin(2 * math.pi * freq1 * xf + phase1 + drift) + \
                    amp2 * math.sin(2 * math.pi * freq2 * xf + phase2 - drift * 0.6)
                path.lineTo(x, y)
            path.lineTo(w, h)
            path.lineTo(0, h)
            path.closeSubpath()

            color = band['color']
            if self._bg_darken > 0:
                color = _lerp_color(color, COLOR_BLACK, self._bg_darken)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawPath(path)

    def _particle_alpha(self, p: 'CalmParticle') -> float:
        alpha = p.opacity_factor
        if p.fade_mult < 1.0:
            alpha *= p.fade_mult
        if p.role != 'field':
            alpha *= p.reveal_alpha
        if self._phase == 'blackout':
            t = min(1.0, self._phase_t / BLACKOUT_SEC)
            alpha *= max(0.0, 1.0 - t)
        return max(0.0, min(1.0, alpha))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        p.setOpacity(self._intro_alpha)

        self._draw_wave_background(p, w, h)

        center = self._center()

        if self._phase in ('intro', 'inhale', 'hold', 'exhale'):
            glow_boost = 1.25 if self._phase == 'hold' else 1.0
            glow_r = max(60.0, self._breathing_max_radius() * 0.5)
            glow = QRadialGradient(center.x(), center.y(), glow_r)
            glow_color = QColor(COLOR_DARK_SAPPHIRE)
            glow_color.setAlpha(int(50 * glow_boost))
            glow.setColorAt(0.0, glow_color)
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillRect(0, 0, w, h, QBrush(glow))

        p.setPen(Qt.NoPen)
        for particle in self._particles:
            if not particle.active:
                continue
            alpha = self._particle_alpha(particle)
            if alpha <= 0.001:
                continue

            size = particle.size
            if particle.role == 'eye':
                size = particle.size * max(0.15, particle.reveal_scale)


            # Soft glow/bloom halo, then the small solid core — a "point of
            # light" rather than a hard flat dot.
            glow_c = QColor(particle.color)
            glow_c.setAlphaF(max(0.0, min(1.0, alpha * GLOW_ALPHA_MULT)))
            p.setBrush(glow_c)
            p.drawEllipse(QPointF(particle.x, particle.y), size * GLOW_SIZE_MULT, size * GLOW_SIZE_MULT)

            core_c = QColor(particle.color)
            core_c.setAlphaF(alpha)
            p.setBrush(core_c)
            p.drawEllipse(QPointF(particle.x, particle.y), size, size)

        if self._phase in ('inhale', 'hold', 'exhale'):
            label = {'inhale': 'INHALE', 'hold': 'HOLD', 'exhale': 'EXHALE'}[self._phase]
            p.save()
            font = QFont('Georgia', 20, QFont.Light)
            p.setFont(font)
            text_c = QColor(COLOR_TEXT)
            text_c.setAlpha(200)
            p.setPen(text_c)
            text_y = h * 0.5 + self._breathing_max_radius() + 70
            p.drawText(QRectF(0, text_y, w, 40), Qt.AlignCenter, label)
            p.restore()

        p.setOpacity(1.0)

    # ── Safe close ──────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._anim_timer.stop()
        super().closeEvent(event)


# ── Quick manual test ───────────────────────────────────────────────────────

if __name__ == '__main__':
    app = QApplication(sys.argv)
    mode = CalmMode()
    mode.finished.connect(lambda: (print('Calm Mode done'), app.quit()))
    mode.start()
    sys.exit(app.exec_())