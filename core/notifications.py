"""
core/notifications.py — cross-platform gradient popup notifications.

Windows compatibility fixes:
  • WA_TranslucentBackground REMOVED — caused invisible windows on Windows DWM
  • Uses setWindowOpacity() for fade-in/out instead (works on all platforms)
  • Solid opaque background with manually painted rounded-rect + clipping mask
  • Qt.Tool replaced with Qt.SubWindow fallback for Windows
  • Explicit Qt.QueuedConnection on signal so cross-thread calls always queue properly
  • push() can be called from ANY thread safely
"""

import threading
import time
import random
import subprocess
import math
from typing import List, Optional

from PyQt5.QtWidgets import QWidget, QApplication
from PyQt5.QtCore    import Qt, QTimer, QObject, pyqtSignal, pyqtSlot, QRect, QPoint
from PyQt5.QtGui     import (
    QPainter, QColor, QLinearGradient, QFont,
    QPen, QBrush, QPainterPath, QRegion,
)

# ─────────────────────────────────────────────────────────────────────────────
#  Messages
# ─────────────────────────────────────────────────────────────────────────────

MSGS = {
    'camera': [
        "Camera blocked! Posture & stress monitoring paused.",
        "Can't see you! Please unblock the camera.",
        "Face not detected for 8 seconds — is the camera covered?",
        "Camera view blocked. Adjust your position to resume monitoring.",
    ],
    'posture': [
        "Your spine called — it wants its dignity back!",
        "Architects draw straight lines. So should you.",
        "You're not a question mark — straighten out!",
        "Less turtle neck, more swan neck. Chin up!",
        "Posture police on duty. Step away from the slouch!",
        "Your spine is doing acrobatics it never signed up for.",
        "Gravity wins again. Fight back — sit straight!",
        "The Eiffel Tower stands straight. Be the tower.",
        "Chair is supportive. Your spine deserves the same.",
    ],
    'stress': [
        "Your face is tighter than an 11:59 PM deadline.",
        "Breathe like the ocean — in, out, in, out.",
        "Coffee can wait. Reset your nervous system first.",
        "Bubble wrap OR deep breath? Breath wins every time.",
        "Stress levels: spicy. Remedy: one deep breath.",
        "Cool it like a cucumber. You're doing great!",
        "Tension detected! Time to discharge some energy.",
        "Even snipers breathe before the shot. Breathe.",
    ],
    'appreciation': [
        "5 minutes of perfect posture! You absolute legend!",
        "Posture Champion unlocked! Your spine thanks you!",
        "WOW! Sitting like royalty for 5 whole minutes!",
        "Look at you — maintaining posture like a total PRO!",
        "Gold medal posture! Your chiropractor weeps with joy!",
        "Perfect alignment streak! That's what we're talking about!",
        "Blooming with good posture energy. Keep it up, champion!",
    ],
    'water': [
        "Hydration station calling! Time for that H2O hit!",
        "Your brain is 73% water. Don't let it become a raisin.",
        "Coffee counts as dehydration. Drink WATER!",
        "Your kidneys slid a note: please hydrate.",
        "1% dehydration = 10% less brainpower. Drink up!",
    ],
    'horizon': [
        "Still looking this way? Find something far off instead.",
        "Eyes still here! Try that distant spot you picked.",
        "This screen isn't 'far away.' Look past it for a moment.",
    ],
}

# Signal-specific messages — used when the alert carries a known specific signal.
# Shown instead of the generic pool above so the popup tells the user exactly what was detected.
SIGNAL_MSGS = {
    'brow_contract': [
        "Brow tension detected — you're furrowing hard. Smooth it out.",
        "Your brows are crunching together. Release that forehead.",
        "Eyebrow furrow spotted — unclench the brow muscles now.",
        "Frowning hard at the screen? Relax those inner brows.",
    ],
    'brow_lower': [
        "Brows are pulling downward — a sign of mental strain. Lift them.",
        "Brow descent detected — soften your forehead and breathe.",
        "Your brows dropped noticeably. Take a breath and release.",
        "Heavy brow tension spotted. Let your forehead go smooth.",
    ],
    'eye_narrow': [
        "Eyes narrowing — squinting at the screen? Blink and look away.",
        "Eye strain detected. Follow 20-20-20: look 20 ft away for 20 s.",
        "Squinting spotted. Increase screen brightness or zoom in.",
        "Your eyes are straining. Give them a 20-second break.",
    ],
    'blink_low': [
        "Blink rate dropped — you're staring too hard. Blink intentionally!",
        "Under-blinking detected. Your eyes are drying out — blink!",
        "Stare mode engaged. Consciously blink 10 times right now.",
        "Low blink rate: screen hypnosis in progress. Snap out, blink!",
    ],
    'blink_high': [
        "Rapid blinking detected — nervous tension? Take a slow breath.",
        "High blink rate spotted — a nervous system response. Breathe.",
        "Blinking fast. Your body is signalling tension — pause & reset.",
    ],
    'lip_press': [
        "Lips pressed tight — jaw tension detected. Unclench your jaw.",
        "Jaw clenching spotted. Drop your tongue from the roof of your mouth.",
        "Lip tension detected. Open your mouth slightly and relax the jaw.",
        "You're holding tension in your jaw. Let it go — breathe.",
    ],
    'mouth_down': [
        "Mouth corners pulling down — relax your facial muscles.",
        "Downturned mouth detected. Your face is mirroring your stress.",
        "Facial tension in your mouth corners. Soften your expression.",
    ],
    'forward_head': [
        "Head drifting forward — you're leaning into the screen!",
        "Forward head posture detected. Pull back and sit tall.",
        "Tech neck starting! Bring your head back over your shoulders.",
        "Your head is creeping toward the screen. Sit back.",
    ],
    'head_droop': [
        "Head drooping forward — chin is dropping. Lift your gaze.",
        "Head droop detected. Lengthen the back of your neck upward.",
        "Text neck posture spotted. Chin up, ears over shoulders.",
        "Your head is drooping. Imagine a string pulling your crown up.",
    ],
    'rounded_shld': [
        "Shoulders rounding forward — open up your chest.",
        "Rounded shoulder posture detected. Draw shoulders back and down.",
        "Hunching spotted! Roll your shoulders back and breathe in.",
        "Shoulder slump detected. Retract and depress those shoulder blades.",
    ],
    'elevated_shld': [
        "Shoulders raised toward your ears — shrug them down now.",
        "Elevated shoulders detected — a classic stress posture. Drop them.",
        "Your shoulders are up around your ears! Release them downward.",
        "Shoulder elevation spotted. Take a breath and let them drop.",
    ],
    'tech_neck': [
        "Tech neck detected — ears are ahead of your shoulders.",
        "Head is too far forward. Tuck your chin and straighten up.",
        "Classic tech neck posture. Retract your head — ears over shoulders.",
        "Cervical strain risk! Your ears should be above your shoulders.",
    ],
}

VOICE_TEXT = {
    'posture':      "Please sit straight",
    'stress':       "Relax. Take a deep breath",
    'appreciation': "Great job! Good posture maintained!",
    'water':        "Break time! Drink some water!",
    'camera':       "Camera blocked. Please uncover your camera.",
    'horizon':      "Please look away from the screen",
}

THEMES = {
    'camera': {
        'top': QColor(200, 100,  10),  'bot': QColor(140,  60,   0),
        'border': QColor(255, 160, 60), 'badge_bg': QColor(255, 140, 30, 60),
        'badge': 'CAMERA BLOCKED',    'text': QColor(255, 220, 160),
    },
    'posture': {
        'top': QColor(190, 20,  35),  'bot': QColor(120,  8, 20),
        'border': QColor(255, 80, 80), 'badge_bg': QColor(255, 60, 60, 60),
        'badge': 'POSTURE ALERT',     'text': QColor(255, 200, 200),
    },
    'stress': {
        'top': QColor(200, 110,  0),  'bot': QColor(140, 60,  0),
        'border': QColor(255, 180, 40), 'badge_bg': QColor(255, 160, 0, 60),
        'badge': 'STRESS DETECTED',   'text': QColor(255, 230, 160),
    },
    'appreciation': {
        'top': QColor(14, 155,  75),  'bot': QColor(8, 100, 50),
        'border': QColor(60, 220, 120), 'badge_bg': QColor(50, 200, 100, 60),
        'badge': 'GREAT JOB!',        'text': QColor(190, 255, 215),
    },
    'water': {
        'top': QColor(30, 100, 215),  'bot': QColor(15,  55, 155),
        'border': QColor(80, 160, 255), 'badge_bg': QColor(60, 130, 255, 60),
        'badge': 'HYDRATION TIME',    'text': QColor(190, 220, 255),
    },
    'horizon': {
        # Calm indigo/blue-violet, matching Horizon Mode's own dark
        # night-sky palette rather than any of the existing alert
        # colors — this should read as "gentle reminder", not "alert".
        'top': QColor(70, 60, 130),   'bot': QColor(40, 34, 82),
        'border': QColor(140, 130, 210), 'badge_bg': QColor(120, 110, 200, 60),
        'badge': 'LOOK AWAY',         'text': QColor(215, 210, 245),
    },
}


# Human-readable labels for each detector signal
SIGNAL_LABELS = {
    'forward_head':   'Forward Head',
    'head_droop':     'Head Droop',
    'rounded_shld':   'Rounded Shoulders',
    'elevated_shld':  'Elevated Shoulders',
    'tech_neck':      'Tech Neck',
    'blink_low':      'Low Blink Rate',
    'blink_high':     'Rapid Blinking',
    'eye_narrow':     'Eye Strain / Squinting',
    'brow_contract':  'Eyebrow Strain',
    'brow_lower':     'Brow Tension',
    'lip_press':      'Jaw / Lip Tension',
    'mouth_down':     'Downturned Mouth',
}

class NotifPopup(QWidget):
    W         = 440
    H         = 138
    MARGIN    = 16
    RADIUS    = 14
    ANIM_MS   = 350    # fade-in / fade-out duration in ms
    HOLD_MS   = 6500   # how long the popup stays fully visible

    def __init__(self, kind: str, msg: str, signals: list = None, persistent: bool = False):
        # ── Window flags: FramelessWindowHint + StaysOnTop ──────────────────
        # Do NOT use Qt.Tool on Windows — it can make the window invisible.
        # Do NOT use WA_TranslucentBackground — breaks DWM compositing.
        # Use a plain frameless window + setWindowOpacity for fading.
        flags = (Qt.FramelessWindowHint |
                 Qt.WindowStaysOnTopHint |
                 Qt.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)

        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # WA_TranslucentBackground intentionally NOT set (Windows fix)
        self.setFixedSize(self.W, self.H)
        self.setWindowOpacity(0.0)   # start invisible; we fade via setWindowOpacity

        self._alive      = True
        self._msg        = msg
        self._theme      = THEMES.get(kind, THEMES['posture'])
        self._t0         = time.time()
        # persistent=True: skip the automatic hold->out transition below
        # (used by Horizon Mode's gaze notice, which must stay open for
        # exactly as long as external state says "looking at screen" —
        # not a fixed duration). Caller must call begin_dismiss() to
        # start the close animation.
        self._persistent = persistent
        # Build subtitle from signal list
        if signals:
            labels = [SIGNAL_LABELS.get(s, s.replace('_', ' ').title()) for s in signals[:2]]
            self._subtitle = '  ·  '.join(labels)
        else:
            self._subtitle = ''

        # Compute final resting position
        screen   = QApplication.primaryScreen().availableGeometry()
        self._tx = screen.right() - self.W - self.MARGIN
        self._ty = screen.top()   + self.MARGIN

        # Start off the right edge of the screen
        self.move(screen.right() + self.W, self._ty)

        self._phase     = 'in'
        self._phase_ms  = 0
        self._timer     = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def show_animated(self):
        self.show()
        self.raise_()
        self._phase_ms = 0
        self._timer.start(16)          # ~60 fps tick

    def _close_safe(self):
        self._alive = False
        self._timer.stop()
        self.hide()
        self.deleteLater()

    def begin_dismiss(self):
        """
        Externally triggered close — used for persistent popups (see
        __init__) once the caller's state says it's time to hide.
        Reuses the same slide-out/fade-out animation as a normal
        timed dismissal, just started on demand instead of after
        HOLD_MS. Safe to call even mid slide-in.
        """
        if self._phase in ('in', 'hold'):
            self._phase    = 'out'
            self._phase_ms = 0

    def _tick(self):
        self._phase_ms += 16

        if self._phase == 'in':
            frac = min(self._phase_ms / self.ANIM_MS, 1.0)
            ease = 1 - (1 - frac) ** 3          # ease-out cubic
            # Slide in from the right
            off_x = int((1 - ease) * (self.W + self.MARGIN + 20))
            self.move(self._tx + off_x, self._ty)
            self.setWindowOpacity(ease)
            if frac >= 1.0:
                self._phase    = 'hold'
                self._phase_ms = 0

        elif self._phase == 'hold':
            self.move(self._tx, self._ty)
            self.setWindowOpacity(1.0)
            if not self._persistent and self._phase_ms >= self.HOLD_MS:
                self._phase    = 'out'
                self._phase_ms = 0

        elif self._phase == 'out':
            frac = min(self._phase_ms / self.ANIM_MS, 1.0)
            ease = frac ** 2                    # ease-in quad
            off_x = int(ease * (self.W + self.MARGIN + 20))
            self.move(self._tx + off_x, self._ty)
            self.setWindowOpacity(1.0 - ease)
            if frac >= 1.0:
                self._close_safe()
                return

        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        W, H  = self.W, self.H
        R     = self.RADIUS
        t     = self._theme
        secs  = time.time() - self._t0

        # ── Background gradient ───────────────────────────────────────────────
        bg = QLinearGradient(0, 0, 0, H)
        bg.setColorAt(0.0, t['top'])
        bg.setColorAt(1.0, t['bot'])
        path = QPainterPath()
        path.addRoundedRect(0, 0, W, H, R, R)
        p.fillPath(path, QBrush(bg))

        # ── Subtle top-shine overlay ──────────────────────────────────────────
        shine = QLinearGradient(0, 0, 0, H // 2)
        shine.setColorAt(0.0, QColor(255, 255, 255, 30))
        shine.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, QBrush(shine))

        # ── Pulsing border ────────────────────────────────────────────────────
        pulse = 0.5 + 0.5 * math.sin(secs * 5.0)
        bc    = QColor(t['border'])
        bc.setAlpha(int(100 + 100 * pulse))
        p.setPen(QPen(bc, 2.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(1, 1, W - 2, H - 2, R, R)

        # ── Badge ─────────────────────────────────────────────────────────────
        badge_bg = QColor(t['badge_bg'])
        p.setBrush(badge_bg)
        p.setPen(Qt.NoPen)
        badge_w = len(t['badge']) * 7 + 20
        p.drawRoundedRect(12, 9, badge_w, 18, 5, 5)

        f1 = QFont(); f1.setPointSize(7); f1.setBold(True); f1.setLetterSpacing(QFont.AbsoluteSpacing, 0.8)
        p.setFont(f1)
        p.setPen(QPen(QColor(t['text'])))
        p.drawText(QRect(12, 9, badge_w, 18), Qt.AlignCenter, t['badge'])

        # ── Divider ───────────────────────────────────────────────────────────
        div_c = QColor(t['text']); div_c.setAlpha(50)
        p.setPen(QPen(div_c, 1))
        p.drawLine(12, 32, W - 12, 32)

        # ── Subtitle (detected signal labels) ─────────────────────────────────
        if self._subtitle:
            f_sub = QFont(); f_sub.setPointSize(10); f_sub.setBold(False)
            p.setFont(f_sub)
            sub_c = QColor(t['text']); sub_c.setAlpha(200)
            p.setPen(QPen(sub_c))
            p.drawText(QRect(14, 35, W - 28, 20), Qt.AlignLeft | Qt.AlignVCenter, self._subtitle)
            msg_top = 58
        else:
            msg_top = 35

        # ── Message text — word-wrapped ───────────────────────────────────────
        words = self._msg.split()
        lines, cur = [], ''
        for w in words:
            test = (cur + ' ' + w).strip()
            if len(test) <= 50:
                cur = test
            else:
                lines.append(cur); cur = w
        if cur:
            lines.append(cur)

        f2 = QFont(); f2.setPointSize(10); f2.setBold(True)
        p.setFont(f2)
        p.setPen(QPen(QColor(255, 255, 255, 235)))
        line_h = 24
        total_h = len(lines[:2]) * line_h
        y_start = msg_top + (H - msg_top - total_h) // 2
        for i, line in enumerate(lines[:2]):
            p.drawText(QRect(14, y_start + i * line_h, W - 28, line_h),
                       Qt.AlignLeft | Qt.AlignVCenter, line)

        p.end()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self._close_safe()

    def mousePressEvent(self, ev):
        """Click to dismiss."""
        self._close_safe()


# ─────────────────────────────────────────────────────────────────────────────
#  NotificationOverlay — thread-safe dispatcher
# ─────────────────────────────────────────────────────────────────────────────

class NotificationOverlay(QObject):
    """
    Lives on the Qt main thread.
    push() is safe to call from ANY thread — the signal uses Qt.QueuedConnection
    so it always marshals delivery to the main thread regardless of caller.
    """

    # str, str, PyQt_PyObject allows passing a Python list across threads safely
    _do_show = pyqtSignal(str, str, 'PyQt_PyObject')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.voice_enabled = True
        self.voice_gender  = 'female'
        self._popups: List[NotifPopup] = []
        self._horizon_popup: Optional[NotifPopup] = None   # persistent, state-driven (see set_horizon_active)
        # Qt.QueuedConnection ensures the slot runs on THIS object's thread (main thread)
        # even when emit() is called from a background thread
        self._do_show.connect(self._create_popup, Qt.QueuedConnection)

    def start(self):
        pass

    def stop(self):
        for p in list(self._popups):
            try:
                if getattr(p, '_alive', False):
                    p._close_safe()
            except Exception:
                pass
        self._popups.clear()
        if self._horizon_popup is not None:
            try:
                self._horizon_popup._close_safe()
            except Exception:
                pass
            self._horizon_popup = None

    def set_horizon_active(self, looking_at_screen: bool):
        """
        Persistent, state-driven notification for Horizon Mode's gaze
        monitor. Unlike push(), this is NOT a one-shot fire-and-forget
        alert: it shows immediately the instant `looking_at_screen`
        becomes True (no cooldown, no delay) and dismisses immediately
        the instant it becomes False (no fixed HOLD_MS) — it continuously
        reflects current gaze state rather than firing a timed popup.

        Main-thread only (no queued-signal marshaling like push() does) —
        callers must invoke this from the Qt main thread. tray_app.py's
        _poll_detector, which is what calls this, already runs there
        (it's a QTimer.timeout slot).
        """
        if looking_at_screen and self._horizon_popup is None:
            msg = random.choice(MSGS.get('horizon', ['Alert!']))
            popup = NotifPopup('horizon', msg, persistent=True)
            self._horizon_popup = popup
            popup.show_animated()
        elif not looking_at_screen and self._horizon_popup is not None:
            self._horizon_popup.begin_dismiss()
            self._horizon_popup = None

    def push(self, kind: str, signals: list = None):
        """Thread-safe — can be called from any thread.

        Message priority:
          1. If a known specific signal is in the list → use its tailored message.
          2. Otherwise fall back to the generic pool for this alert kind.
        This ensures e.g. 'brow_contract' always shows a brow-specific message,
        not a generic posture/stress message.
        """
        sigs = signals or []
        msg  = None

        # Pick the most specific message based on the first recognised signal
        for sig in sigs:
            pool = SIGNAL_MSGS.get(sig)
            if pool:
                msg = random.choice(pool)
                break

        if msg is None:
            msg = random.choice(MSGS.get(kind, ['Alert!']))

        self._do_show.emit(kind, msg, sigs)   # queued → always delivers on main thread
        if self.voice_enabled:
            threading.Thread(target=self._speak, args=(kind,),
                             daemon=True).start()

    def set_voice(self, enabled: bool, gender: str):
        self.voice_enabled = enabled
        self.voice_gender  = gender

    @pyqtSlot(str, str, 'PyQt_PyObject')
    def _create_popup(self, kind: str, msg: str, signals: list):
        """Always runs on Qt main thread — safe to create/show widgets."""
        live = []
        for p in self._popups:
            try:
                if getattr(p, '_alive', False):
                    live.append(p)
            except Exception:
                pass
        self._popups = live
        popup = NotifPopup(kind, msg, signals)
        self._popups.append(popup)
        popup.show_animated()

    def _speak(self, kind: str):
        text   = VOICE_TEXT.get(kind, 'Alert!')
        female = self.voice_gender == 'female'

        # ── Windows PowerShell SAPI ───────────────────────────────────────────
        # Female: try named voices in order — Zira (Win10/11, warm natural US
        # female), Hazel (UK female), Eva (Win8 female) — then gender hint.
        # Male: David (Win10/11 natural US male), Mark → gender hint.
        # Rate -2 = calm unhurried. Volume 85 = clear but soft.
        if female:
            win_ps = (
                'Add-Type -AssemblyName System.Speech; '
                '$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
                '$voices = @("Microsoft Zira Desktop","Microsoft Hazel Desktop",'
                '"Microsoft Eva Mobile","Microsoft Zira","Microsoft Hazel"); '
                '$picked = $false; '
                'foreach ($v in $voices) { '
                '  try { $s.SelectVoice($v); $picked = $true; break } catch {} } '
                'if (-not $picked) { '
                '  $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female) } '
                '$s.Rate = -2; $s.Volume = 85; '
                f'$s.Speak("{text}");'
            )
        else:
            win_ps = (
                'Add-Type -AssemblyName System.Speech; '
                '$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
                '$voices = @("Microsoft David Desktop","Microsoft Mark Desktop",'
                '"Microsoft David","Microsoft Mark"); '
                '$picked = $false; '
                'foreach ($v in $voices) { '
                '  try { $s.SelectVoice($v); $picked = $true; break } catch {} } '
                'if (-not $picked) { '
                '  $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Male) } '
                '$s.Rate = -2; $s.Volume = 85; '
                f'$s.Speak("{text}");'
            )

        win_cmd = ['powershell', '-WindowStyle', 'Hidden', '-NonInteractive',
                   '-Command', win_ps]

        # ── macOS `say` ───────────────────────────────────────────────────────
        # Female: Samantha (warm US English) — rate 165 wpm (default ~200)
        # Male  : Daniel   (calm British)    — rate 165 wpm
        # Both lower than default so they sound unhurried and gentle.
        if female:
            mac_voice, mac_rate = 'Samantha', '165'
        else:
            mac_voice, mac_rate = 'Daniel', '165'

        mac_cmd = ['say', '-v', mac_voice, '-r', mac_rate, text]

        # ── Linux espeak ──────────────────────────────────────────────────────
        # Pitch: female 62 (softer, slightly higher), male 44 (deeper, calm)
        # Speed: 130 wpm (default 175) — slower feels calmer
        # Amplitude: 120 (default 200) — quieter = softer
        if female:
            esp_pitch, esp_speed, esp_amp = '62', '130', '120'
        else:
            esp_pitch, esp_speed, esp_amp = '44', '130', '120'

        esp_cmd = [
            'espeak',
            '-p', esp_pitch,
            '-s', esp_speed,
            '-a', esp_amp,
            text,
        ]

        # ── Linux festival ────────────────────────────────────────────────────
        # festival has limited voice control via CLI; we pass through as-is.
        fest_cmd = ['bash', '-c', f'echo "{text}" | festival --tts']

        for cmd in [win_cmd, mac_cmd, esp_cmd, fest_cmd]:
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return
            except (FileNotFoundError, OSError):
                continue