"""
ui/tray_app.py — Canary 🐦  (v3 — clean rebuild)

Changes vs previous version:
  • No Unplug mode (removed entirely)
  • Alerts fire ONLY on posture/stress STATE CHANGE (new → active or active → cleared)
    — no repeated alerts for the same sustained condition
  • Camera-blocked / face-not-detected alert after 8 s of no detection while session active
  • Live dashboard tab with real-time metrics + timestamp timeline
  • Session results tab with compare-to-history analysis
  • Settings tab (voice only — no Unplug)
"""

import sys, os, time, math, json
from pathlib    import Path
from datetime   import datetime
from typing     import Optional, List, Dict, Any
from collections import defaultdict

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QSystemTrayIcon, QMenu, QAction,
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QStackedWidget, QScrollArea, QGridLayout, QSizePolicy,
    QGraphicsDropShadowEffect, QCheckBox, QProgressBar, QMessageBox,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QRect, QObject, QRectF
from PyQt5.QtGui  import (
    QIcon, QPixmap, QPainter, QColor, QLinearGradient,
    QFont, QPen, QBrush, QPainterPath, QRadialGradient,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.detector      import StressPostureDetector, SessionData
from core.notifications import NotificationOverlay
from core import history_db


# ══════════════════════════════════════════════════════════════════════════════
#  Palette
# ══════════════════════════════════════════════════════════════════════════════

C = {
    'bg':       '#070b14',
    'card':     '#0d1321',
    'card2':    '#111827',
    'border':   '#1e2a3a',
    'red':      '#ef4444',  'red_d':    '#991b1b',
    'yellow':   '#f59e0b',  'yellow_d': '#92400e',
    'green':    '#10b981',  'green_d':  '#065f46',
    'blue':     '#3b82f6',  'blue_d':   '#1e3a8a',
    'purple':   '#8b5cf6',  'purple_d': '#4c1d95',
    'orange':   '#f97316',
    'canary':   '#fbbf24',  'canary_d': '#92400e',
    'text':     '#e2e8f0',
    'muted':    '#64748b',
    'muted2':   '#94a3b8',
}

_SETTINGS_FILE = Path.home() / '.canary_settings.json'

# Alert dedup: once an alert fires, don't fire the same kind again until
# the signal clears AND then re-appears.
CAMERA_BLOCKED_TIMEOUT = 8.0   # seconds of no-face before camera-blocked alert
CAMERA_ALERT_COOLDOWN  = 30.0  # don't spam camera alerts

HORIZON_INTERVAL_MS      = 20 * 60 * 1000  # 20-20-20 rule: every 20 minutes during an active session
HORIZON_LOOKAWAY_COOLDOWN = 15.0            # seconds between "please look away" nudges while Horizon Mode is active

# ── Calm Mode — automatic high-stress trigger (Phase 3) ────────────────────
# NOTE: these are starting points for real-world tuning, not scientifically
# derived thresholds. They gate an EDGE (rising above threshold, falling
# below recovery) rather than reacting to any single frame.
CALM_STRESS_THRESHOLD  = 0.65   # stress_score that can start the countdown to an automatic Calm
CALM_STRESS_DURATION   = 60.0   # seconds stress must stay >= CALM_STRESS_THRESHOLD before Calm actually fires
CALM_RECOVERY_LEVEL    = 0.40   # stress must drop below this to re-arm automatic Calm
CALM_RECOVERY_DURATION = 20.0   # seconds stress must stay below CALM_RECOVERY_LEVEL to re-arm

# ── Unplug Mode — automatic alert-threshold trigger (Phase 3) ──────────────
UNPLUG_ALERT_THRESHOLD = 30        # meaningful (edge-triggered) posture/stress alerts before Unplug is considered
UNPLUG_COOLDOWN        = 30 * 60   # seconds after an AUTOMATIC Unplug before another automatic one can fire

# ── Calm → recovery → Unplug chain (Phase 3) ────────────────────────────────
CALM_UNPLUG_RECOVERY_INTERVAL  = 90.0             # seconds to wait after an alert-triggered Calm before reassessing stress
POST_CALM_STILL_ELEVATED_LEVEL = CALM_STRESS_THRESHOLD  # stress at/above this after the recovery interval → launch Unplug


# ══════════════════════════════════════════════════════════════════════════════
#  Settings
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_SETTINGS = {
    'voice_enabled': True,
    'voice_gender':  'female',
    'unplug_enabled': True,
    'unplug_gender':  'boy',
    # NOTE: no SettingsPage checkbox wires these up yet (Phase 3 scope was
    # orchestration, not settings UI) — they default on so automatic Horizon/
    # Calm behave exactly as before until a toggle is added. See report.
    'horizon_enabled': True,
    'calm_enabled':    True,
}

def _load_settings() -> dict:
    try:
        if _SETTINGS_FILE.exists():
            saved  = json.loads(_SETTINGS_FILE.read_text())
            merged = dict(DEFAULT_SETTINGS); merged.update(saved)
            return merged
    except Exception: pass
    return dict(DEFAULT_SETTINGS)

def _save_settings(s: dict):
    try: _SETTINGS_FILE.write_text(json.dumps(s, indent=2))
    except Exception: pass


# ══════════════════════════════════════════════════════════════════════════════
#  History
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
#  History (Phase 4: SQLite-backed — see core/history_db.py)
# ══════════════════════════════════════════════════════════════════════════════

# How many recent completed sessions to keep in memory for the dashboard's
# comparison/trend views (SessionResultsPage). The database itself keeps
# every session forever; this only bounds what's loaded into the UI at once.
HISTORY_MEMORY_CAP = 200

def _load_history() -> List[dict]:
    return history_db.fetch_sessions(limit=HISTORY_MEMORY_CAP)

def _session_to_dict(sd: SessionData, extra: Optional[dict] = None) -> dict:
    """
    `extra` carries the Phase-3/Unplug data SessionData itself has no way
    to know about (meaningful-alert count, Horizon/Calm/Unplug usage during
    THIS session) — see TrayApp._finish_stopping_session, which is the only
    caller that passes it.
    """
    d = {
        'start':            sd.start_time,
        'end':              sd.end_time,
        'duration_min':     sd.duration_min,
        'good_posture_pct': sd.good_posture_pct,
        'avg_stress':       sd.avg_stress,
        'avg_posture':      sd.avg_posture,
        'avg_blink_rate':   sd.avg_blink_rate,
        'posture_alerts':   sd.posture_alerts,
        'stress_alerts':    sd.stress_alerts,
        'appreciation':     sd.appreciation_alerts,
        'water':            sd.water_alerts,
        'meaningful_alerts':          0,
        'horizon_uses':               0,
        'horizon_secs':               0.0,
        'calm_uses':                  0,
        'calm_secs':                  0.0,
        'unplug_uses':                0,
        'unplug_secs':                0.0,
        'unplug_exercises_completed': 0,
        'events':           [
            {
                'ts':      e.timestamp,
                'kind':    e.kind,
                'score':   e.score,
                'signals': e.signals,
            }
            for e in sd.events
        ],
    }
    if extra:
        d.update(extra)
    return d


# ══════════════════════════════════════════════════════════════════════════════
#  Generic helpers
# ══════════════════════════════════════════════════════════════════════════════

def _label(text, size=13, color=C['text'], bold=False, align=Qt.AlignLeft):
    lb = QLabel(text)
    f  = lb.font(); f.setPointSize(size); f.setBold(bold); lb.setFont(f)
    lb.setStyleSheet(f'color:{color}; background:transparent;')
    lb.setAlignment(align); lb.setWordWrap(True)
    return lb

def _sep():
    f = QFrame(); f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f'color:{C["border"]}; background:{C["border"]};'
                    f'max-height:1px;'); return f


# ══════════════════════════════════════════════════════════════════════════════
#  GlowCard
# ══════════════════════════════════════════════════════════════════════════════

class GlowCard(QFrame):
    def __init__(self, glow_color=C['canary'], parent=None):
        super().__init__(parent)
        self._gc = QColor(glow_color); self._alpha=50; self._step=0.0
        self.setMinimumHeight(70)
        self.setStyleSheet(f"""GlowCard{{background:{C['card']};
            border-radius:14px;border:1.5px solid {C['border']};}}""")
        t = QTimer(self); t.timeout.connect(self._pulse); t.start(55)

    def _pulse(self):
        self._step  = (self._step + 0.09) % (2*3.14159)
        self._alpha = int(35 + 32*math.sin(self._step)); self.update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self._gc); c.setAlpha(self._alpha)
        p.setPen(QPen(c,2)); p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(self.rect().adjusted(1,1,-1,-1),13,13)


# ══════════════════════════════════════════════════════════════════════════════
#  Charts
# ══════════════════════════════════════════════════════════════════════════════

class DonutChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self.setMinimumSize(150,150)
        self._slices=[]; self._label=''

    def set_data(self,s,l=''): self._slices=s; self._label=l; self.update()

    def paintEvent(self,_):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        w,h=self.width(),self.height(); r=min(w,h)-16
        rect=QRect((w-r)//2,(h-r)//2,r,r)
        total=sum(v for v,_ in self._slices) or 1; ang=-90*16
        for val,col in self._slices:
            span=int(val/total*360*16); p.setBrush(QColor(col)); p.setPen(Qt.NoPen)
            p.drawPie(rect,ang,span); ang+=span
        inner=int(r*0.60); ir=QRect((w-inner)//2,(h-inner)//2,inner,inner)
        p.setBrush(QColor(C['card'])); p.setPen(Qt.NoPen); p.drawEllipse(ir)
        f=QFont(); f.setPointSize(13); f.setBold(True); p.setFont(f)
        p.setPen(QColor(C['text'])); p.drawText(ir,Qt.AlignCenter,self._label)


class BarChart(QWidget):
    BASELINE_Y=28; TOP_PAD=24; SIDE_PAD=20; BAR_GAP=14

    def __init__(self,parent=None):
        super().__init__(parent); self.setMinimumHeight(180); self._bars=[]

    def set_data(self,b): self._bars=b; self.update()

    def paintEvent(self,_):
        if not self._bars: return
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W,H=self.width(),self.height(); n=len(self._bars)
        sp,gap=self.SIDE_PAD,self.BAR_GAP; bl_y=H-self.BASELINE_Y
        avail_h=bl_y-self.TOP_PAD
        max_val=max((v for _,v,_ in self._bars),default=1) or 1
        bw=max(8,(W-2*sp-(n-1)*gap)//n)
        p.setPen(QPen(QColor(C['border']),1)); p.drawLine(sp,bl_y,W-sp,bl_y)
        for i,(lbl,val,col) in enumerate(self._bars):
            x=sp+i*(bw+gap); bh=max(4,int(val/max_val*avail_h)); by=bl_y-bh
            grad=QLinearGradient(0,by,0,bl_y); c=QColor(col)
            grad.setColorAt(0.0,c.lighter(130)); grad.setColorAt(1.0,c)
            path=QPainterPath(); path.addRoundedRect(QRectF(x,by,bw,bh),5,5)
            p.fillPath(path,QBrush(grad))
            fv=QFont(); fv.setPointSize(9); fv.setBold(True); p.setFont(fv)
            p.setPen(QColor(C['text']))
            p.drawText(QRect(x,by-20,bw,18),Qt.AlignCenter,str(int(val)))
            fl=QFont(); fl.setPointSize(8); p.setFont(fl)
            p.setPen(QColor(C['muted']))
            p.drawText(QRect(x,bl_y+4,bw,self.BASELINE_Y-4),
                       Qt.AlignCenter|Qt.TextWordWrap,lbl)


class CompareChart(QWidget):
    BASELINE_Y=32;TOP_PAD=28;SIDE_PAD=24;GROUP_GAP=22;BAR_GAP=4;BAR_W=18

    def __init__(self,parent=None):
        super().__init__(parent); self.setMinimumHeight(200); self._groups=[]

    def set_data(self,g): self._groups=g; self.update()

    def paintEvent(self,_):
        if not self._groups: return
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W,H=self.width(),self.height(); sp=self.SIDE_PAD
        bl_y=H-self.BASELINE_Y; avail_h=bl_y-self.TOP_PAD
        bw=self.BAR_W; gg=self.GROUP_GAP; bg=self.BAR_GAP; n=len(self._groups)
        p.setPen(QPen(QColor(C['border']),1)); p.drawLine(sp,bl_y,W-sp,bl_y)
        group_w=bw*2+bg; total_w=n*group_w+(n-1)*gg; x_start=(W-total_w)//2
        for i,(lbl,prev,curr,pc,cc,mx) in enumerate(self._groups):
            gx=x_start+i*(group_w+gg); mx=mx or 1
            for j,(val,col) in enumerate([(prev,pc),(curr,cc)]):
                bh=max(2,int(val/mx*avail_h)); by=bl_y-bh; bx=gx+j*(bw+bg)
                grad=QLinearGradient(0,by,0,bl_y); c=QColor(col)
                grad.setColorAt(0.0,c.lighter(130)); grad.setColorAt(1.0,c)
                path=QPainterPath(); path.addRoundedRect(QRectF(bx,by,bw,bh),3,3)
                p.fillPath(path,QBrush(grad))
                f=QFont(); f.setPointSize(7); f.setBold(True); p.setFont(f)
                p.setPen(QColor(C['text']))
                p.drawText(QRect(bx,by-16,bw,14),Qt.AlignCenter,str(int(val)))
            f2=QFont(); f2.setPointSize(8); p.setFont(f2)
            p.setPen(QColor(C['muted']))
            p.drawText(QRect(gx,bl_y+4,group_w,self.BASELINE_Y-4),
                       Qt.AlignCenter|Qt.TextWordWrap,lbl)
        f3=QFont(); f3.setPointSize(8); p.setFont(f3); lx=W-110
        for j,(label,col) in enumerate([('Previous',C['muted2']),('Current',C['canary'])]):
            p.setBrush(QColor(col)); p.setPen(Qt.NoPen)
            p.drawRoundedRect(lx,8+j*14,10,8,2,2)
            p.setPen(QColor(C['muted']))
            p.drawText(lx+14,8+j*14,80,10,Qt.AlignLeft|Qt.AlignVCenter,label)


# ══════════════════════════════════════════════════════════════════════════════
#  StatTile + delta badge
# ══════════════════════════════════════════════════════════════════════════════

class StatTile(GlowCard):
    def __init__(self,icon,label,value,unit='',glow_color=C['canary']):
        super().__init__(glow_color); self.setMinimumHeight(95)
        ly=QVBoxLayout(self); ly.setContentsMargins(16,12,16,12); ly.setSpacing(3)
        top=QHBoxLayout(); top.addWidget(_label(icon,size=18)); top.addStretch()
        top.addWidget(_label(label,size=9,color=C['muted'])); ly.addLayout(top)
        vr=QHBoxLayout()
        self._vl=_label(str(value),size=26,color=glow_color,bold=True)
        self._vl.setStyleSheet(f'color:{glow_color};background:transparent;')
        vr.addWidget(self._vl)
        if unit: vr.addWidget(_label(unit,size=11,color=C['muted']))
        vr.addStretch(); ly.addLayout(vr)

    def set_value(self,v): self._vl.setText(str(v))


def _delta_badge(curr,prev,higher_is_better=True)->QLabel:
    if prev is None:
        lb=QLabel('  —  ')
        lb.setStyleSheet(f'color:{C["muted"]};background:transparent;font-size:10pt;')
        return lb
    diff=curr-prev
    if abs(diff)<0.5:    txt,col='  ≈ same  ',C['muted2']
    elif(diff>0)==higher_is_better: txt,col=f'  ▲ {abs(diff):.1f}  ',C['green']
    else:                txt,col=f'  ▼ {abs(diff):.1f}  ',C['red']
    lb=QLabel(txt)
    lb.setStyleSheet(f'color:{col};background:{col}22;border-radius:6px;'
                     f'padding:1px 6px;font-size:10pt;font-weight:bold;')
    return lb


# ══════════════════════════════════════════════════════════════════════════════
#  RecCard
# ══════════════════════════════════════════════════════════════════════════════

class RecCard(QFrame):
    def __init__(self,icon,badge_color,badge,title,text):
        super().__init__()
        self.setStyleSheet(f"""RecCard{{background:{C['card2']};border-radius:12px;
            border:1px solid {badge_color}44;}}""")
        ly=QHBoxLayout(self); ly.setContentsMargins(14,12,14,12); ly.setSpacing(12)
        ly.addWidget(_label(icon,size=20))
        right=QVBoxLayout(); right.setSpacing(3)
        bdg=QLabel(badge)
        bdg.setStyleSheet(f"""QLabel{{color:{badge_color};background:{badge_color}22;
            border:1px solid {badge_color}55;border-radius:6px;
            padding:1px 8px;font-size:9pt;font-weight:bold;}}""")
        bdg.setMaximumWidth(220); right.addWidget(bdg)
        right.addWidget(_label(title,size=11,bold=True))
        t=QLabel(text); t.setWordWrap(True)
        t.setStyleSheet(f'color:{C["muted2"]};font-size:10pt;background:transparent;')
        right.addWidget(t); ly.addLayout(right)


# ══════════════════════════════════════════════════════════════════════════════
#  Live mini gauge
# ══════════════════════════════════════════════════════════════════════════════

class MiniGauge(QWidget):
    """Horizontal progress bar with label + value."""
    def __init__(self, label, color, parent=None):
        super().__init__(parent)
        self._color = color
        self.setFixedHeight(48)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0,0,0,0); lay.setSpacing(2)
        hrow = QHBoxLayout()
        self._lbl = QLabel(label)
        self._lbl.setStyleSheet(f'color:{C["muted2"]};font-size:9pt;background:transparent;')
        hrow.addWidget(self._lbl)
        hrow.addStretch()
        self._val = QLabel('—')
        self._val.setStyleSheet(f'color:{color};font-size:10pt;font-weight:bold;background:transparent;')
        hrow.addWidget(self._val)
        lay.addLayout(hrow)
        self._bar = QProgressBar()
        self._bar.setRange(0,100); self._bar.setValue(0)
        self._bar.setTextVisible(False); self._bar.setFixedHeight(6)
        self._bar.setStyleSheet(f"""
            QProgressBar{{background:{C['border']};border-radius:3px;border:none;}}
            QProgressBar::chunk{{background:{color};border-radius:3px;}}""")
        lay.addWidget(self._bar)

    def update_value(self, pct: float, display: str):
        self._bar.setValue(int(max(0, min(100, pct*100))))
        self._val.setText(display)


# ══════════════════════════════════════════════════════════════════════════════
#  Timeline event row
# ══════════════════════════════════════════════════════════════════════════════

class EventRow(QFrame):
    KIND_STYLE = {
        'posture':     (C['red'],    '🔴', 'POSTURE'),
        'stress':      (C['yellow'], '🟡', 'STRESS'),
        'appreciation':(C['green'],  '🟢', 'GREAT!'),
        'water':       (C['blue'],   '💧', 'HYDRATION'),
        'camera':      (C['orange'], '📷', 'CAMERA'),
    }
    def __init__(self, ts: float, kind: str, detail: str, session_start: float):
        super().__init__()
        col, ico, badge = self.KIND_STYLE.get(kind, (C['muted2'], '•', kind.upper()))
        self.setStyleSheet(f'QFrame{{background:{C["card2"]};border-radius:8px;'
                           f'border-left:3px solid {col};}}')
        ly = QHBoxLayout(self); ly.setContentsMargins(10,6,10,6); ly.setSpacing(8)
        elapsed = ts - session_start
        m, s = int(elapsed//60), int(elapsed%60)
        ts_str = f'{datetime.fromtimestamp(ts).strftime("%H:%M:%S")}  +{m:02d}:{s:02d}'
        ly.addWidget(_label(ico, size=14))
        bdg = QLabel(badge)
        bdg.setStyleSheet(f'color:{col};background:{col}22;border-radius:4px;'
                          f'padding:0 6px;font-size:8pt;font-weight:bold;')
        ly.addWidget(bdg)
        ly.addWidget(_label(ts_str, size=8, color=C['muted']))
        ly.addStretch()
        if detail:
            ly.addWidget(_label(detail, size=8, color=C['muted2']))


# ══════════════════════════════════════════════════════════════════════════════
#  Live Dashboard Page
# ══════════════════════════════════════════════════════════════════════════════

class LiveDashboardPage(QWidget):
    """Real-time metrics + event timeline shown while a session is running."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f'background:{C["bg"]};')
        self._session_start = 0.0
        self._events: List[dict] = []   # {'ts', 'kind', 'detail'}
        self._history: List[dict] = []  # previous sessions for comparison
        self._build_ui()

    def _build_ui(self):
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            'QScrollArea{border:none;background:transparent;}'
            f'QScrollBar:vertical{{background:{C["card"]};width:6px;border-radius:3px;}}'
            f'QScrollBar::handle:vertical{{background:#8b5cf6;border-radius:3px;}}')
        inner = QWidget(); inner.setStyleSheet(f'background:{C["bg"]};')
        self._iv = QVBoxLayout(inner)
        self._iv.setContentsMargins(24,20,24,24); self._iv.setSpacing(16)
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)

        # ── Inactive placeholder (shown when no session) ──────────────────────
        self._placeholder = QWidget()
        ph_ly = QVBoxLayout(self._placeholder)
        ph_ly.setAlignment(Qt.AlignCenter); ph_ly.setSpacing(14)
        ph_ly.addWidget(_label('🐦', size=48, align=Qt.AlignCenter))
        ph_ly.addWidget(_label('No active session', size=15, color=C['muted'],
                               align=Qt.AlignCenter))
        ph_ly.addWidget(_label('Start a session to see live metrics.',
                               size=10, color=C['muted'], align=Qt.AlignCenter))

        # ── Status banner ─────────────────────────────────────────────────────
        self._status_card = GlowCard(C['green'])
        self._status_card.setMinimumHeight(0)
        sc_ly = QHBoxLayout(self._status_card)
        sc_ly.setContentsMargins(18,10,18,10)
        self._status_ico  = QLabel('✅')
        self._status_ico.setStyleSheet('font-size:20pt;background:transparent;')
        self._status_txt  = QLabel('Session active — calibrating…')
        self._status_txt.setStyleSheet(f'color:{C["text"]};font-size:12pt;'
                                       f'font-weight:bold;background:transparent;')
        self._status_time = QLabel('00:00')
        self._status_time.setStyleSheet(f'color:{C["muted"]};font-size:11pt;'
                                        f'background:transparent;')
        sc_ly.addWidget(self._status_ico)
        sc_ly.addWidget(self._status_txt)
        sc_ly.addStretch()
        sc_ly.addWidget(self._status_time)

        # ── Gauge row ─────────────────────────────────────────────────────────
        gauge_card = GlowCard(C['purple'])
        gauge_card.setMinimumHeight(0)
        gc_ly = QVBoxLayout(gauge_card)
        gc_ly.setContentsMargins(18,14,18,14); gc_ly.setSpacing(8)
        gc_ly.addWidget(_label('LIVE METRICS', size=8, color=C['muted']))
        self._g_posture = MiniGauge('Posture Score', C['red'])
        self._g_stress  = MiniGauge('Stress Score',  C['yellow'])
        self._g_blink   = MiniGauge('Blink Rate',    C['canary'])
        for g in (self._g_posture, self._g_stress, self._g_blink):
            gc_ly.addWidget(g)

        # ── Active signals ────────────────────────────────────────────────────
        sig_card = GlowCard(C['blue']); sig_card.setMinimumHeight(0)
        sc2_ly = QVBoxLayout(sig_card)
        sc2_ly.setContentsMargins(18,14,18,14); sc2_ly.setSpacing(6)
        sc2_ly.addWidget(_label('ACTIVE SIGNALS', size=8, color=C['muted']))
        self._signals_row = QHBoxLayout(); self._signals_row.setSpacing(6)
        self._signals_row.addWidget(_label('None detected', size=9, color=C['muted']))
        sc2_ly.addLayout(self._signals_row)

        # ── Comparison card (vs same hour previous sessions) ──────────────────
        self._comp_card = GlowCard(C['canary']); self._comp_card.setMinimumHeight(0)
        self._comp_ly = QVBoxLayout(self._comp_card)
        self._comp_ly.setContentsMargins(18,14,18,14); self._comp_ly.setSpacing(6)

        # ── Event timeline ────────────────────────────────────────────────────
        tl_card = GlowCard(C['border']); tl_card.setMinimumHeight(0)
        tl_ly = QVBoxLayout(tl_card)
        tl_ly.setContentsMargins(18,14,18,14); tl_ly.setSpacing(6)
        tl_ly.addWidget(_label('EVENT TIMELINE', size=8, color=C['muted']))
        self._tl_inner = QVBoxLayout(); self._tl_inner.setSpacing(4)
        tl_ly.addLayout(self._tl_inner)

        # assemble
        self._iv.addWidget(self._status_card)
        self._iv.addWidget(gauge_card)
        self._iv.addWidget(sig_card)
        self._iv.addWidget(self._comp_card)
        self._iv.addWidget(tl_card)
        self._iv.addStretch()
        scroll.setWidget(inner)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._placeholder)   # 0
        self._stack.addWidget(scroll)               # 1
        outer.addWidget(self._stack)

    def set_inactive(self):
        self._stack.setCurrentIndex(0)

    def set_active(self, session_start: float, history: List[dict]):
        self._session_start = session_start
        self._events = []
        self._history = history
        self._clear_timeline()
        self._rebuild_comparison()
        self._stack.setCurrentIndex(1)
        self._status_txt.setText('Session active — calibrating…')
        self._status_ico.setText('⏳')

    def on_calibrated(self):
        self._status_txt.setText('Session active — monitoring')
        self._status_ico.setText('✅')

    def _clear_timeline(self):
        while self._tl_inner.count():
            item = self._tl_inner.takeAt(0)
            if item.widget(): item.widget().deleteLater()

    def add_event(self, ts: float, kind: str, detail: str):
        self._events.append({'ts': ts, 'kind': kind, 'detail': detail})
        row = EventRow(ts, kind, detail, self._session_start)
        # Insert at top (most recent first)
        self._tl_inner.insertWidget(0, row)
        # Keep max 50 visible rows
        while self._tl_inner.count() > 50:
            item = self._tl_inner.takeAt(self._tl_inner.count()-1)
            if item.widget(): item.widget().deleteLater()

    def update_live(self, live: dict):
        """Called from main thread every ~1s with fresh detector data."""
        # Status timer
        elapsed = time.time() - self._session_start
        m, s = int(elapsed//60), int(elapsed%60)
        self._status_time.setText(f'{m:02d}:{s:02d}')

        if live.get('calibrated'):
            self.on_calibrated()

        # Gauges
        ps = live.get('posture_score', 0)
        ss = live.get('stress_score', 0)
        br = live.get('blink_rate', 0)
        self._g_posture.update_value(ps, f'{ps:.0%}')
        self._g_stress.update_value(ss,  f'{ss:.0%}')
        br_pct = min(br / 20, 1.0)
        self._g_blink.update_value(br_pct, f'{br:.0f}/min')

        # Signals
        sigs = live.get('active_signals', [])
        SIGNAL_LABELS = {
            'forward_head': 'Fwd Head', 'head_droop': 'Head Droop',
            'rounded_shld': 'Rounded Shld', 'elevated_shld': 'Elevated Shld',
            'tech_neck': 'Tech Neck', 'blink_low': 'Low Blink',
            'blink_high': 'Rapid Blink', 'eye_narrow': 'Eye Strain',
            'brow_contract': 'Brow Strain', 'brow_lower': 'Brow Tension',
            'lip_press': 'Lip Tension', 'mouth_down': 'Mouth Tension',
        }
        while self._signals_row.count():
            item = self._signals_row.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        if sigs:
            for sig in sigs[:6]:
                lbl = SIGNAL_LABELS.get(sig, sig)
                tag = QLabel(lbl)
                col = C['red'] if sig in ('forward_head','head_droop','rounded_shld',
                                          'elevated_shld','tech_neck') else C['yellow']
                tag.setStyleSheet(f'color:{col};background:{col}22;border-radius:4px;'
                                  f'padding:1px 6px;font-size:8pt;font-weight:bold;')
                self._signals_row.addWidget(tag)
        else:
            self._signals_row.addWidget(_label('None detected', size=9, color=C['muted']))
        self._signals_row.addStretch()

    def _rebuild_comparison(self):
        """Build a "how do you compare at this hour" section from history."""
        while self._comp_ly.count():
            item = self._comp_ly.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        self._comp_ly.addWidget(_label('COMPARE TO USUAL  (same time of day)',
                                       size=8, color=C['muted']))

        if not self._history:
            self._comp_ly.addWidget(
                _label('No past sessions yet — patterns will appear here after your first session.',
                       size=9, color=C['muted']))
            return

        now_hour = datetime.now().hour
        # Gather sessions that started within ±2 hours of current time
        same_hour = [s for s in self._history
                     if abs(datetime.fromtimestamp(s.get('start', 0)).hour - now_hour) <= 2]

        if not same_hour:
            self._comp_ly.addWidget(
                _label(f'No previous sessions around {now_hour:02d}:00 — keep going!',
                       size=9, color=C['muted']))
            return

        avg_posture_pct = sum(s.get('good_posture_pct',0) for s in same_hour) / len(same_hour)
        avg_posture_al  = sum(s.get('posture_alerts',0)   for s in same_hour) / len(same_hour)
        avg_stress_al   = sum(s.get('stress_alerts',0)    for s in same_hour) / len(same_hour)
        avg_blink       = sum(s.get('avg_blink_rate',0)   for s in same_hour) / len(same_hour)

        self._comp_ly.addWidget(
            _label(f'Based on {len(same_hour)} session(s) around {now_hour:02d}:00 — '
                   f'here is what you usually do:', size=9, color=C['muted2']))

        row = QHBoxLayout(); row.setSpacing(8)
        for lbl, val, unit, col in [
            ('Typical Good Posture', f'{avg_posture_pct:.0f}', '%',    C['green']),
            ('Typical Posture Alerts', f'{avg_posture_al:.1f}', 'avg', C['red']),
            ('Typical Stress Alerts',  f'{avg_stress_al:.1f}',  'avg', C['yellow']),
            ('Typical Blink Rate',     f'{avg_blink:.0f}',      '/min',C['canary']),
        ]:
            card = GlowCard(col); card.setMinimumHeight(0)
            cl = QVBoxLayout(card); cl.setContentsMargins(10,8,10,8); cl.setSpacing(1)
            cl.addWidget(_label(lbl, size=7, color=C['muted']))
            vrow = QHBoxLayout()
            vrow.addWidget(_label(val, size=16, bold=True, color=col))
            vrow.addWidget(_label(unit, size=8, color=C['muted']))
            vrow.addStretch()
            cl.addLayout(vrow)
            row.addWidget(card)
        self._comp_ly.addLayout(row)


# ══════════════════════════════════════════════════════════════════════════════
#  Recommendations
# ══════════════════════════════════════════════════════════════════════════════

def _recommendations(sd: SessionData, prev: Optional[dict]) -> list:
    recs=[]
    if sd.posture_alerts>3:
        recs.append(('🪑',C['red'],'🔴  Needs Work','Posture needs improvement',
            f'{sd.posture_alerts} posture alerts. Try the 90-90-90 rule: '
            'hips, knees, ankles at 90°. Monitor at eye level.'))
    elif sd.posture_alerts==0 and sd.duration_min>3:
        recs.append(('🏆',C['green'],'🟢  Keep It Up','Stellar posture discipline!',
            'Zero posture alerts. Your spine is thriving. '
            'Consistency builds lasting muscle memory.'))
    if sd.stress_alerts>2:
        recs.append(('🧘',C['yellow'],'🟡  Needs Work','Manage facial tension',
            f'{sd.stress_alerts} stress spikes. '
            'Try 4-7-8 breathing: inhale 4s, hold 7s, exhale 8s.'))
    br=sd.avg_blink_rate
    if 0<br<10:
        recs.append(('👁️',C['blue'],'🔵  Pro Tip','Blink more often',
            f'Blink rate ~{int(br)}/min (healthy: 12-20). '
            '20-20-20 rule: every 20 min, look 20 ft away for 20 s.'))
    if sd.good_posture_pct>70:
        recs.append(('⭐',C['canary'],'🟡  Excellent',
            f'{sd.good_posture_pct}% good posture',
            "Elite consistency — building habits that compound over months."))
    if prev:
        delta=sd.good_posture_pct-prev.get('good_posture_pct',sd.good_posture_pct)
        if delta>=5:
            recs.append(('📈',C['green'],'🟢  Improving',
                f'Good posture up {delta:.0f}% vs last session!',
                'Great trend. Keep the momentum going.'))
        elif delta<=-5:
            recs.append(('📉',C['yellow'],'🟡  Slipped a bit',
                f'Good posture down {abs(delta):.0f}% vs last session.',
                'No worries — awareness is the first step.'))
    recs.append(('🚶',C['blue'],'🔵  Pro Tip','Micro-breaks are powerful',
        '2-min walk every 45-60 min boosts creativity by up to 81% (Stanford).'))
    return recs


# ══════════════════════════════════════════════════════════════════════════════
#  Session Results Page
# ══════════════════════════════════════════════════════════════════════════════

class SessionResultsPage(QWidget):
    clear_history_requested = pyqtSignal()

    def __init__(self,parent=None):
        super().__init__(parent); self.setStyleSheet(f'background:{C["bg"]};')
        self._scroll=QScrollArea(); self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            'QScrollArea{border:none;background:transparent;}'
            f'QScrollBar:vertical{{background:{C["card"]};width:6px;border-radius:3px;}}'
            f'QScrollBar::handle:vertical{{background:#8b5cf6;border-radius:3px;}}')
        outer=QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)
        outer.addWidget(self._scroll); self._show_placeholder()

    def _show_placeholder(self):
        w=QWidget(); w.setStyleSheet(f'background:{C["bg"]};')
        lv=QVBoxLayout(w); lv.setAlignment(Qt.AlignCenter); lv.setSpacing(14)
        lv.addWidget(_label('🐦',size=48,align=Qt.AlignCenter))
        lv.addWidget(_label('No session data yet',size=15,color=C['muted'],align=Qt.AlignCenter))
        lv.addWidget(_label('Start a session to see your Canary report.',
                             size=10,color=C['muted'],align=Qt.AlignCenter))
        self._scroll.setWidget(w)

    def _confirm_clear_history(self):
        reply = QMessageBox.question(
            self, 'Clear Session History',
            'This permanently deletes all STORED session history '
            '(comparisons, trends, and the previous-sessions list).\n\n'
            'It will NOT affect a session currently in progress.\n\n'
            'This cannot be undone. Continue?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.clear_history_requested.emit()

    def load(self,sd:SessionData,history:List[dict]):
        prev=history[-2] if len(history)>=2 else None
        w=QWidget(); w.setStyleSheet(f'background:{C["bg"]};')
        lv=QVBoxLayout(w); lv.setContentsMargins(24,20,24,24); lv.setSpacing(20)

        hdr=QHBoxLayout()
        hdr.addWidget(_label('🐦  Session Report',size=15,bold=True))
        hdr.addStretch()
        mins=int(sd.duration_min); secs=int((sd.duration_min-mins)*60)
        hdr.addWidget(_label(f'Duration: {mins}m {secs}s',size=10,color=C['muted']))
        clear_btn = QPushButton('🗑  Clear History')
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet(f"""QPushButton{{background:transparent;color:{C['muted']};
            border:1px solid {C['border']};border-radius:6px;padding:4px 12px;font-size:9pt;}}
            QPushButton:hover{{color:{C['red']};border-color:{C['red']};}}""")
        clear_btn.clicked.connect(self._confirm_clear_history)
        hdr.addSpacing(12); hdr.addWidget(clear_btn)
        lv.addLayout(hdr)

        # Session timestamp
        start_str = datetime.fromtimestamp(sd.start_time).strftime('%a %d %b %Y  %H:%M:%S')
        end_str   = datetime.fromtimestamp(sd.end_time).strftime('%H:%M:%S')
        lv.addWidget(_label(f'🕐  {start_str}  →  {end_str}', size=9, color=C['muted']))

        lv.addWidget(_label('OVERVIEW',size=8,color=C['muted']))
        grid=QGridLayout(); grid.setSpacing(10)
        for i,(ico,lbl,val,unit,col) in enumerate([
            ('🪑','Posture Alerts',sd.posture_alerts,'',C['red']),
            ('😤','Stress Alerts',sd.stress_alerts,'',C['yellow']),
            ('✅','Good Posture',sd.good_posture_pct,'%',C['green']),
            ('💧','Water Breaks',sd.water_alerts,'',C['blue']),
            ('👁️','Avg Blink/min',sd.avg_blink_rate,'',C['canary']),
            ('🌟','Appreciation',sd.appreciation_alerts,'',C['green']),
        ]):
            grid.addWidget(StatTile(ico,lbl,val,unit,col),i//3,i%3)
        lv.addLayout(grid)

        lv.addWidget(_label('POSTURE DISTRIBUTION',size=8,color=C['muted']))
        cr=QHBoxLayout(); cr.setSpacing(14)
        donut=DonutChart()
        donut.set_data([
            (max(sd.good_posture_secs,0),C['green']),
            (max(sd.bad_posture_secs,0),C['red']),
            (max(sd.no_detection_secs,0),C['muted']),
        ],f'{int(sd.good_posture_pct)}%')
        dc=GlowCard('#8b5cf6'); dc.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Preferred)
        dl=QVBoxLayout(dc); dl.setContentsMargins(14,12,14,12)
        dl.addWidget(_label('Posture Quality',size=11,bold=True))
        leg=QHBoxLayout()
        for col,txt in [(C['green'],'Good'),(C['red'],'Poor'),(C['muted'],'No detect')]:
            dot=QLabel('●'); dot.setStyleSheet(f'color:{col};background:transparent;font-size:12pt;')
            leg.addWidget(dot); leg.addWidget(_label(txt,size=9,color=C['muted'])); leg.addSpacing(6)
        leg.addStretch(); dl.addLayout(leg); dl.addWidget(donut); cr.addWidget(dc,stretch=1)

        bar=BarChart(); bar.set_data([
            ('Posture',sd.posture_alerts,C['red']),
            ('Stress',sd.stress_alerts,C['yellow']),
            ('Appre.',sd.appreciation_alerts,C['green']),
            ('Water',sd.water_alerts,C['blue']),
        ])
        bc=GlowCard('#8b5cf6'); bc.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Preferred)
        bl=QVBoxLayout(bc); bl.setContentsMargins(14,12,14,12)
        bl.addWidget(_label('Alert Breakdown',size=11,bold=True))
        bl.addWidget(bar); cr.addWidget(bc,stretch=1); lv.addLayout(cr)

        lv.addWidget(_label('SESSION COMPARISON',size=8,color=C['muted']))
        comp_card=GlowCard(C['canary']); comp_card.setMinimumHeight(260)
        comp_layout=QVBoxLayout(comp_card)
        comp_layout.setContentsMargins(14,12,14,12); comp_layout.setSpacing(10)
        if prev is None:
            comp_layout.addWidget(_label('No previous session to compare yet.',size=11,color=C['muted']))
        else:
            prev_start = datetime.fromtimestamp(prev.get('start',0)).strftime('%d %b %H:%M')
            comp_layout.addWidget(_label(f'Current vs Previous Session  ({prev_start})',size=11,bold=True))
            delta_row=QHBoxLayout(); delta_row.setSpacing(16)
            for metric,curr_v,prev_v,hib in [
                ('Good Posture %',sd.good_posture_pct,prev.get('good_posture_pct',0),True),
                ('Posture Alerts',sd.posture_alerts,prev.get('posture_alerts',0),False),
                ('Stress Alerts',sd.stress_alerts,prev.get('stress_alerts',0),False),
                ('Avg Blink/min',sd.avg_blink_rate,prev.get('avg_blink_rate',0),True),
            ]:
                mc=GlowCard(C['canary']); mc.setMinimumHeight(0)
                ml=QVBoxLayout(mc); ml.setContentsMargins(12,8,12,8); ml.setSpacing(2)
                ml.addWidget(_label(metric,size=8,color=C['muted']))
                vrow=QHBoxLayout()
                vrow.addWidget(_label(str(round(curr_v,1)),size=16,bold=True,color=C['text']))
                vrow.addWidget(_delta_badge(curr_v,prev_v,hib)); vrow.addStretch()
                ml.addLayout(vrow)
                ml.addWidget(_label(f'prev: {round(prev_v,1)}',size=8,color=C['muted']))
                delta_row.addWidget(mc)
            comp_layout.addLayout(delta_row)
            mx=max(sd.posture_alerts,sd.stress_alerts,
                   prev.get('posture_alerts',0),prev.get('stress_alerts',0),1)
            cmp=CompareChart(); cmp.set_data([
                ('Posture\nAlerts',prev.get('posture_alerts',0),sd.posture_alerts,C['muted2'],C['red'],mx),
                ('Stress\nAlerts',prev.get('stress_alerts',0),sd.stress_alerts,C['muted2'],C['yellow'],mx),
                ('Good\nPosture%',prev.get('good_posture_pct',0),sd.good_posture_pct,C['muted2'],C['green'],100),
                ('Blink\n/min',prev.get('avg_blink_rate',0),sd.avg_blink_rate,C['muted2'],C['canary'],
                 max(sd.avg_blink_rate,prev.get('avg_blink_rate',0),20)),
            ]); comp_layout.addWidget(cmp)
        lv.addWidget(comp_card)

        # ── Time-of-day comparison from full history ───────────────────────────
        now_hour = datetime.fromtimestamp(sd.start_time).hour
        same_hour = [s for s in history[:-1]
                     if abs(datetime.fromtimestamp(s.get('start',0)).hour - now_hour) <= 2]
        if same_hour:
            lv.addWidget(_label(f'SAME-HOUR PATTERNS  (sessions around {now_hour:02d}:00)',
                                size=8, color=C['muted']))
            sh_card = GlowCard(C['purple']); sh_card.setMinimumHeight(0)
            sh_ly = QVBoxLayout(sh_card); sh_ly.setContentsMargins(14,12,14,12)
            avg_pp = sum(s.get('good_posture_pct',0) for s in same_hour)/len(same_hour)
            avg_pa = sum(s.get('posture_alerts',0)   for s in same_hour)/len(same_hour)
            avg_sa = sum(s.get('stress_alerts',0)    for s in same_hour)/len(same_hour)
            sh_ly.addWidget(_label(
                f'Across {len(same_hour)} session(s) around this hour:  '
                f'Avg good posture {avg_pp:.0f}%  •  '
                f'Avg posture alerts {avg_pa:.1f}  •  '
                f'Avg stress alerts {avg_sa:.1f}',
                size=9, color=C['muted2']))

            # How did this session compare?
            if sd.good_posture_pct >= avg_pp + 5:
                sh_ly.addWidget(_label('📈  Better than usual at this time — great work!',
                                       size=10, bold=True, color=C['green']))
            elif sd.good_posture_pct <= avg_pp - 5:
                sh_ly.addWidget(_label('📉  Posture was below your usual for this time slot.',
                                       size=10, bold=True, color=C['yellow']))
            else:
                sh_ly.addWidget(_label('≈  Consistent with your usual performance at this hour.',
                                       size=10, bold=True, color=C['muted2']))
            lv.addWidget(sh_card)

        # ── Progress over time (Phase 4) ────────────────────────────────────────
        # "Hourly/weekly/monthly progress" — interpreted as rolling-window
        # summaries (today / this week / this month) over the full stored
        # session history, since a single Canary session already spans well
        # beyond one calendar hour. All-time totals come from the SAME
        # `history` list (now backed by history_db, unbounded — see
        # HISTORY_MEMORY_CAP), not from a fixed most-recent-20 cutoff.
        lv.addWidget(_label('PROGRESS OVER TIME',size=8,color=C['muted']))
        now_ts = sd.end_time
        windows = [
            ('Today',      24*3600,      C['blue']),
            ('This Week',  7*24*3600,    C['purple']),
            ('This Month', 30*24*3600,   C['canary']),
        ]
        prog_row = QHBoxLayout(); prog_row.setSpacing(10)
        for label, span, col in windows:
            bucket = [s for s in history if now_ts - s.get('start', 0) <= span]
            gc = GlowCard(col); gl3 = QVBoxLayout(gc); gl3.setContentsMargins(14,10,14,10); gl3.setSpacing(2)
            gl3.addWidget(_label(label, size=9, color=C['muted']))
            gl3.addWidget(_label(f'{len(bucket)} session(s)', size=14, bold=True, color=col))
            if bucket:
                avg_gp = sum(s.get('good_posture_pct',0) for s in bucket)/len(bucket)
                total_ma = sum(s.get('meaningful_alerts',0) for s in bucket)
                gl3.addWidget(_label(f'Avg good posture {avg_gp:.0f}%  •  {total_ma} meaningful alert(s)',
                                      size=8, color=C['muted2']))
            else:
                gl3.addWidget(_label('No sessions yet', size=8, color=C['muted2']))
            prog_row.addWidget(gc)
        lv.addLayout(prog_row)

        # Overall trend: most recent few sessions vs everything before them.
        if len(history) >= 4:
            recent_n = min(5, len(history)//2)
            recent = history[-recent_n:]
            older   = history[:-recent_n]
            avg_recent = sum(s.get('good_posture_pct',0) for s in recent)/len(recent)
            avg_older  = sum(s.get('good_posture_pct',0) for s in older)/len(older)
            trend_card = GlowCard(C['green'] if avg_recent >= avg_older else C['yellow'])
            tl = QVBoxLayout(trend_card); tl.setContentsMargins(14,10,14,10); tl.setSpacing(4)
            tl.addWidget(_label(f'OVERALL TREND  ({len(history)} session(s) on record)', size=8, color=C['muted']))
            if avg_recent >= avg_older + 3:
                tl.addWidget(_label(f'📈  Good posture trending up: {avg_older:.0f}% → {avg_recent:.0f}% (last {recent_n})',
                                     size=10, bold=True, color=C['green']))
            elif avg_recent <= avg_older - 3:
                tl.addWidget(_label(f'📉  Good posture trending down: {avg_older:.0f}% → {avg_recent:.0f}% (last {recent_n})',
                                     size=10, bold=True, color=C['yellow']))
            else:
                tl.addWidget(_label(f'≈  Holding steady around {avg_recent:.0f}% good posture',
                                     size=10, bold=True, color=C['muted2']))
            lv.addWidget(trend_card)

        # Previous sessions list (most recent first, capped for readability)
        if len(history) >= 2:
            lv.addWidget(_label('PREVIOUS SESSIONS',size=8,color=C['muted']))
            hist_card = GlowCard(C['muted2']); hist_card.setMinimumHeight(0)
            hl_ = QVBoxLayout(hist_card); hl_.setContentsMargins(14,10,14,10); hl_.setSpacing(6)
            for s in list(reversed(history[:-1]))[:10]:
                when = datetime.fromtimestamp(s.get('start',0)).strftime('%a %d %b, %H:%M')
                row_ = QHBoxLayout()
                row_.addWidget(_label(when, size=9, color=C['muted2']))
                row_.addWidget(_label(f"{s.get('duration_min',0):.0f} min", size=9, color=C['muted']))
                row_.addWidget(_label(f"{s.get('good_posture_pct',0):.0f}% good posture", size=9, color=C['green']))
                row_.addWidget(_label(f"{s.get('posture_alerts',0)} posture / {s.get('stress_alerts',0)} stress",
                                       size=9, color=C['muted']))
                row_.addStretch()
                hl_.addLayout(row_)
            lv.addWidget(hist_card)

        lv.addWidget(_label('WELLNESS SCORES',size=8,color=C['muted']))
        sc_row=QHBoxLayout(); sc_row.setSpacing(10)
        for lbl,val,col in [
            ('Avg Stress Level',f'{sd.avg_stress:.0f}%',C['yellow']),
            ('Avg Posture Deviation',f'{sd.avg_posture:.0f}%',C['red']),
            ('Blink Health','✅ Normal' if 10<=sd.avg_blink_rate<=22 else '⚠️ Low',C['blue']),
        ]:
            gc=GlowCard(col); gl=QVBoxLayout(gc); gl.setContentsMargins(14,10,14,10)
            gl.addWidget(_label(lbl,size=9,color=C['muted']))
            gl.addWidget(_label(val,size=18,bold=True,color=col)); sc_row.addWidget(gc)
        lv.addLayout(sc_row)

        lv.addWidget(_label('INSIGHTS & RECOMMENDATIONS',size=8,color=C['muted']))
        for args in _recommendations(sd,prev): lv.addWidget(RecCard(*args))
        lv.addStretch(); self._scroll.setWidget(w)


# ══════════════════════════════════════════════════════════════════════════════
#  Mode Statistics Page (Phase 4)
# ══════════════════════════════════════════════════════════════════════════════

class ModeStatsPage(QWidget):
    """
    Reads core/history_db.fetch_mode_usage() directly — every Horizon/Calm/
    Unplug run ever recorded, session or standalone (see TrayApp.
    _record_mode_run). Only shows metrics Canary actually collects: no
    fabricated scores or invented streaks.
    """
    def __init__(self, parent=None):
        super().__init__(parent); self.setStyleSheet(f'background:{C["bg"]};')
        self._scroll = QScrollArea(); self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            'QScrollArea{border:none;background:transparent;}'
            f'QScrollBar:vertical{{background:{C["card"]};width:6px;border-radius:3px;}}'
            f'QScrollBar::handle:vertical{{background:#8b5cf6;border-radius:3px;}}')
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)
        outer.addWidget(self._scroll)
        self.refresh()

    @staticmethod
    def _fmt_secs(total_secs: float) -> str:
        total_secs = int(total_secs)
        if total_secs < 60: return f'{total_secs}s'
        m, s = divmod(total_secs, 60)
        if m < 60: return f'{m}m {s}s'
        h, m = divmod(m, 60)
        return f'{h}h {m}m'

    @staticmethod
    def _fmt_recency(ts: float) -> str:
        delta = time.time() - ts
        if delta < 3600:      return f'{int(delta//60)} min ago'
        if delta < 86400:     return f'{int(delta//3600)} hr ago'
        return datetime.fromtimestamp(ts).strftime('%a %d %b, %H:%M')

    def refresh(self):
        w = QWidget(); w.setStyleSheet(f'background:{C["bg"]};')
        lv = QVBoxLayout(w); lv.setContentsMargins(24,20,24,24); lv.setSpacing(20)
        lv.addWidget(_label('📈  Mode Statistics', size=15, bold=True))
        lv.addWidget(_label('Usage across every Horizon, Calm, and Unplug run — '
                             'manual or automatic, with or without an active session.',
                             size=9, color=C['muted']))

        specs = [
            ('horizon', '👁️', 'Horizon Mode', C['blue']),
            ('calm',    '🌊', 'Calm Mode',    C['purple']),
            ('unplug',  '🔌', 'Unplug Mode',  C['canary']),
        ]
        any_data = False
        for mode, ico, title, col in specs:
            runs = history_db.fetch_mode_usage(mode)
            gc = GlowCard(col); gc.setMinimumHeight(0)
            gl = QVBoxLayout(gc); gl.setContentsMargins(18,16,18,16); gl.setSpacing(10)
            gl.addWidget(_label(f'{ico}  {title}', size=12, bold=True, color=col))

            if not runs:
                gl.addWidget(_label('Not used yet.', size=10, color=C['muted']))
                lv.addWidget(gc)
                continue
            any_data = True

            total_secs   = sum(r['duration_secs'] for r in runs)
            during_sess  = sum(1 for r in runs if r['during_session'])
            standalone   = len(runs) - during_sess
            last_run     = runs[-1]

            grid = QGridLayout(); grid.setSpacing(10)
            grid.addWidget(StatTile('🔁','Times Used',len(runs),'',col), 0, 0)
            grid.addWidget(StatTile('⏱️','Total Time',self._fmt_secs(total_secs),'',col), 0, 1)
            grid.addWidget(StatTile('🕐','Last Used',self._fmt_recency(last_run['start_time']),'',col), 0, 2)
            gl.addLayout(grid)
            gl.addWidget(_label(
                f'{during_sess} during a session  •  {standalone} standalone (manual, no session)',
                size=9, color=C['muted2']))

            if mode == 'calm':
                for_unplug = sum(1 for r in runs if r['extra'].get('for_unplug'))
                gl.addWidget(_label(
                    f'{for_unplug} triggered as stage 1 of an automatic Unplug  •  '
                    f'{len(runs) - for_unplug} manual or standalone high-stress trigger',
                    size=9, color=C['muted2']))

            if mode == 'unplug':
                completed = sum(1 for r in runs if r['completed'])
                exercises = sum(r['extra'].get('exercises_completed', 0) for r in runs)
                automatic_note = sum(1 for r in runs if r['extra'].get('character'))
                if runs:
                    gl.addWidget(_label(
                        f'{completed}/{len(runs)} completed the full exercise routine  •  '
                        f'{exercises} exercise(s) completed in total',
                        size=9, color=C['muted2']))

            lv.addWidget(gc)

        if not any_data:
            lv.addWidget(_label('No Horizon, Calm, or Unplug usage recorded yet.',
                                 size=10, color=C['muted']))

        lv.addStretch()
        self._scroll.setWidget(w)


# ══════════════════════════════════════════════════════════════════════════════
#  Settings Page (voice only — no Unplug)
# ══════════════════════════════════════════════════════════════════════════════

class SettingsPage(QWidget):
    settings_changed = pyqtSignal(dict)

    def __init__(self, settings: dict, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f'background:{C["bg"]};')
        self._s = dict(settings)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            'QScrollArea{border:none;background:transparent;}'
            f'QScrollBar:vertical{{background:{C["card"]};width:6px;border-radius:3px;}}'
            f'QScrollBar::handle:vertical{{background:#8b5cf6;border-radius:3px;}}')
        inner = QWidget(); inner.setStyleSheet(f'background:{C["bg"]};')
        iv = QVBoxLayout(inner); iv.setSpacing(20); iv.setContentsMargins(28,24,28,24)

        def sec_header(ico, title):
            lbl = QLabel(f'{ico}  {title}')
            lbl.setStyleSheet(f'color:{C["muted2"]};font-size:11pt;'
                              f'font-weight:bold;background:transparent;')
            iv.addWidget(lbl)

        def make_card(col=C['canary']):
            gc = GlowCard(col); gc.setMinimumHeight(0); return gc

        def toggle_style():
            return (f'QCheckBox::indicator{{width:48px;height:26px;border-radius:13px;}}'
                    f'QCheckBox::indicator:checked{{background:#8b5cf6;border:2px solid #8b5cf6;}}'
                    f'QCheckBox::indicator:unchecked{{background:#334155;border:2px solid #475569;}}')

        _PG = ('background:qlineargradient(x1:0,y1:0,x2:1,y2:0,'
               'stop:0 #4c1d95,stop:0.4 #7c3aed,stop:1 #a855f7);'
               'color:white;border-color:#a855f7;')

        # ── Voice ──────────────────────────────────────────────────────────────
        sec_header('🔊', 'Voice Alerts')
        vc = make_card(C['blue']); vc.setMinimumHeight(140)
        vl = QVBoxLayout(vc); vl.setContentsMargins(22,20,22,20); vl.setSpacing(18)

        er = QHBoxLayout()
        ei = QVBoxLayout(); ei.setSpacing(3)
        el = QLabel('Enable Voice Alerts')
        el.setStyleSheet(f'color:{C["text"]};font-size:11pt;font-weight:bold;background:transparent;')
        ed = QLabel('System speaks alert messages aloud')
        ed.setStyleSheet(f'color:{C["muted2"]};font-size:10pt;background:transparent;')
        ei.addWidget(el); ei.addWidget(ed)
        er.addLayout(ei); er.addStretch()
        self.voice_chk = QCheckBox()
        self.voice_chk.setChecked(self._s['voice_enabled'])
        self.voice_chk.setStyleSheet(toggle_style())
        er.addWidget(self.voice_chk); vl.addLayout(er)
        vl.addWidget(_sep())

        gr = QHBoxLayout()
        gi = QVBoxLayout(); gi.setSpacing(3)
        gl = QLabel('Voice Gender')
        gl.setStyleSheet(f'color:{C["text"]};font-size:11pt;font-weight:bold;background:transparent;')
        gd = QLabel('Select voice for audio alerts')
        gd.setStyleSheet(f'color:{C["muted2"]};font-size:10pt;background:transparent;')
        gi.addWidget(gl); gi.addWidget(gd)
        gr.addLayout(gi); gr.addStretch()
        self.btn_vf = QPushButton('👩  Female')
        self.btn_vm = QPushButton('👨  Male')
        for btn in (self.btn_vf, self.btn_vm):
            btn.setCheckable(True); btn.setFixedHeight(40)
            btn.setMinimumWidth(110); btn.setCursor(Qt.PointingHandCursor)
            gr.addWidget(btn)
        self.btn_vf.setChecked(self._s['voice_gender']=='female')
        self.btn_vm.setChecked(self._s['voice_gender']=='male')
        self._style_voice(); vl.addLayout(gr)
        iv.addWidget(vc)

        # ── Horizon Mode ───────────────────────────────────────────────────────
        sec_header('👁️', 'Horizon Mode')
        hzc = make_card(C['blue']); hzc.setMinimumHeight(0)
        hzl = QVBoxLayout(hzc); hzl.setContentsMargins(22,20,22,20); hzl.setSpacing(18)
        hzr = QHBoxLayout()
        hzi = QVBoxLayout(); hzi.setSpacing(3)
        hzl_lbl = QLabel('Enable Horizon Mode')
        hzl_lbl.setStyleSheet(f'color:{C["text"]};font-size:11pt;font-weight:bold;background:transparent;')
        hzd = QLabel('20-20-20 eye-rest reminder every 20 minutes during a session')
        hzd.setStyleSheet(f'color:{C["muted2"]};font-size:10pt;background:transparent;')
        hzi.addWidget(hzl_lbl); hzi.addWidget(hzd)
        hzr.addLayout(hzi); hzr.addStretch()
        self.horizon_chk = QCheckBox()
        self.horizon_chk.setChecked(self._s.get('horizon_enabled', True))
        self.horizon_chk.setStyleSheet(toggle_style())
        hzr.addWidget(self.horizon_chk); hzl.addLayout(hzr)
        iv.addWidget(hzc)

        # ── Calm Mode ──────────────────────────────────────────────────────────
        sec_header('🌊', 'Calm Mode')
        clc = make_card(C['purple']); clc.setMinimumHeight(0)
        cll = QVBoxLayout(clc); cll.setContentsMargins(22,20,22,20); cll.setSpacing(18)
        clr = QHBoxLayout()
        cli = QVBoxLayout(); cli.setSpacing(3)
        cll_lbl = QLabel('Enable Calm Mode')
        cll_lbl.setStyleSheet(f'color:{C["text"]};font-size:11pt;font-weight:bold;background:transparent;')
        cld = QLabel('Automatic breathing break during sustained high stress')
        cld.setStyleSheet(f'color:{C["muted2"]};font-size:10pt;background:transparent;')
        cli.addWidget(cll_lbl); cli.addWidget(cld)
        clr.addLayout(cli); clr.addStretch()
        self.calm_chk = QCheckBox()
        self.calm_chk.setChecked(self._s.get('calm_enabled', True))
        self.calm_chk.setStyleSheet(toggle_style())
        clr.addWidget(self.calm_chk); cll.addLayout(clr)
        iv.addWidget(clc)

        # ── Unplug Mode ────────────────────────────────────────────────────────
        sec_header('🔌', 'Unplug Mode')
        uc = make_card(C['purple']); uc.setMinimumHeight(140)
        ul = QVBoxLayout(uc); ul.setContentsMargins(22,20,22,20); ul.setSpacing(18)

        ur = QHBoxLayout()
        ui_ = QVBoxLayout(); ui_.setSpacing(3)
        ul_lbl = QLabel('Enable Unplug Mode')
        ul_lbl.setStyleSheet(f'color:{C["text"]};font-size:11pt;font-weight:bold;background:transparent;')
        ud = QLabel('Take guided exercise breaks when signals repeat')
        ud.setStyleSheet(f'color:{C["muted2"]};font-size:10pt;background:transparent;')
        ui_.addWidget(ul_lbl); ui_.addWidget(ud)
        ur.addLayout(ui_); ur.addStretch()
        self.unplug_chk = QCheckBox()
        self.unplug_chk.setChecked(self._s['unplug_enabled'])
        self.unplug_chk.setStyleSheet(toggle_style())
        ur.addWidget(self.unplug_chk); ul.addLayout(ur)
        ul.addWidget(_sep())

        cr = QHBoxLayout()
        ci = QVBoxLayout(); ci.setSpacing(3)
        cl = QLabel('Exercise Character')
        cl.setStyleSheet(f'color:{C["text"]};font-size:11pt;font-weight:bold;background:transparent;')
        cd = QLabel('Choose who demonstrates exercises during breaks')
        cd.setStyleSheet(f'color:{C["muted2"]};font-size:10pt;background:transparent;')
        ci.addWidget(cl); ci.addWidget(cd)
        cr.addLayout(ci); cr.addStretch()
        self.btn_cg_boy  = QPushButton('👦  Boy')
        self.btn_cg_girl = QPushButton('👧  Girl')
        for btn in (self.btn_cg_boy, self.btn_cg_girl):
            btn.setCheckable(True); btn.setFixedHeight(40)
            btn.setMinimumWidth(110); btn.setCursor(Qt.PointingHandCursor)
            cr.addWidget(btn)
        self.btn_cg_boy.setChecked(self._s['unplug_gender']=='boy')
        self.btn_cg_girl.setChecked(self._s['unplug_gender']=='girl')
        self._style_unplug_gender(); ul.addLayout(cr)
        iv.addWidget(uc)

        # ── Alert Color Guide ──────────────────────────────────────────────────
        sec_header('🎨', 'Alert Color Guide')
        for ico, col, title, desc in [
            ('🔴', C['red'],    'Red — Posture Alert',
             'Fires when posture changes to a bad state (forward head, tech neck, '
             'rounded/elevated shoulders, head droop).\nVoice: "Please sit straight"'),
            ('🟡', C['yellow'], 'Yellow — Stress Alert',
             'Fires when stress signals newly appear (low blink, eye narrowing, '
             'brow tension, lip/mouth tension).\nVoice: "Relax. Take a deep breath"'),
            ('🟢', C['green'],  'Green — Great Work!',
             '5 consecutive minutes of good posture with no stress.\n'
             'Voice: "Great job! Good posture maintained!"'),
            ('💧', C['blue'],   'Blue — Hydration Break',
             'Every 30 minutes — stay hydrated.\nVoice: "Break time! Drink some water!"'),
            ('📷', C['orange'], 'Orange — Camera Blocked',
             'Face/posture not detectable for 8 s while session is active. '
             'Uncover the camera or adjust your position.'),
        ]:
            gc = make_card(col)
            gl2 = QHBoxLayout(gc); gl2.setContentsMargins(16,12,16,12); gl2.setSpacing(14)
            icon_lbl = QLabel(ico)
            icon_lbl.setStyleSheet('font-size:18pt;background:transparent;')
            gl2.addWidget(icon_lbl)
            info = QVBoxLayout(); info.setSpacing(3)
            t1 = QLabel(title)
            t1.setStyleSheet(f'color:{col};font-size:11pt;font-weight:bold;background:transparent;')
            t2 = QLabel(desc); t2.setStyleSheet(
                f'color:{C["muted2"]};font-size:9pt;background:transparent;')
            t2.setWordWrap(True)
            info.addWidget(t1); info.addWidget(t2)
            gl2.addLayout(info); gl2.addStretch()
            iv.addWidget(gc)

        iv.addStretch()
        scroll.setWidget(inner)
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0); outer.addWidget(scroll)

        self.voice_chk.stateChanged.connect(self._emit)
        self.btn_vf.clicked.connect(lambda: self._vgender('female'))
        self.btn_vm.clicked.connect(lambda: self._vgender('male'))
        self.horizon_chk.stateChanged.connect(self._emit)
        self.calm_chk.stateChanged.connect(self._emit)
        self.unplug_chk.stateChanged.connect(self._emit)
        self.btn_cg_boy.clicked.connect(lambda: self._cgender('boy'))
        self.btn_cg_girl.clicked.connect(lambda: self._cgender('girl'))

    def _style_voice(self):
        col = '#8b5cf6'
        _PG = ('background:qlineargradient(x1:0,y1:0,x2:1,y2:0,'
               'stop:0 #4c1d95,stop:0.4 #7c3aed,stop:1 #a855f7);color:white;border-color:#a855f7;')
        for btn, g in [(self.btn_vf,'female'),(self.btn_vm,'male')]:
            on = self._s['voice_gender']==g
            if on:
                btn.setStyleSheet(f'QPushButton{{background:transparent;color:{col};'
                    f'border:2px solid {col};border-radius:8px;padding:7px 20px;'
                    f'font-size:11pt;font-weight:bold;}}QPushButton:hover{{{_PG}}}')
            else:
                btn.setStyleSheet(f'QPushButton{{background:{C["card2"]};color:{C["muted2"]};'
                    f'border:2px solid {C["border"]};border-radius:8px;padding:7px 20px;'
                    f'font-size:11pt;}}QPushButton:hover{{{_PG}}}')

    def _vgender(self, g):
        self._s['voice_gender']=g
        self.btn_vf.setChecked(g=='female'); self.btn_vm.setChecked(g=='male')
        self._style_voice(); self._emit()

    def _style_unplug_gender(self):
        col = '#8b5cf6'
        _PG = ('background:qlineargradient(x1:0,y1:0,x2:1,y2:0,'
               'stop:0 #4c1d95,stop:0.4 #7c3aed,stop:1 #a855f7);color:white;border-color:#a855f7;')
        for btn, g in [(self.btn_cg_boy,'boy'),(self.btn_cg_girl,'girl')]:
            on = self._s['unplug_gender']==g
            if on:
                btn.setStyleSheet(f'QPushButton{{background:transparent;color:{col};'
                    f'border:2px solid {col};border-radius:8px;padding:7px 20px;'
                    f'font-size:11pt;font-weight:bold;}}QPushButton:hover{{{_PG}}}')
            else:
                btn.setStyleSheet(f'QPushButton{{background:{C["card2"]};color:{C["muted2"]};'
                    f'border:2px solid {C["border"]};border-radius:8px;padding:7px 20px;'
                    f'font-size:11pt;}}QPushButton:hover{{{_PG}}}')

    def _cgender(self, g):
        self._s['unplug_gender']=g
        self.btn_cg_boy.setChecked(g=='boy'); self.btn_cg_girl.setChecked(g=='girl')
        self._style_unplug_gender(); self._emit()

    def _emit(self):
        self._s['voice_enabled'] = self.voice_chk.isChecked()
        self._s['horizon_enabled'] = self.horizon_chk.isChecked()
        self._s['calm_enabled'] = self.calm_chk.isChecked()
        self._s['unplug_enabled'] = self.unplug_chk.isChecked()
        self.settings_changed.emit(dict(self._s))

    def get_settings(self) -> dict:
        self._s['voice_enabled'] = self.voice_chk.isChecked()
        self._s['horizon_enabled'] = self.horizon_chk.isChecked()
        self._s['calm_enabled'] = self.calm_chk.isChecked()
        self._s['unplug_enabled'] = self.unplug_chk.isChecked()
        return dict(self._s)


# ══════════════════════════════════════════════════════════════════════════════
#  Dashboard window
# ══════════════════════════════════════════════════════════════════════════════

class DashboardWindow(QMainWindow):
    TABS = [('📡', 'Live'), ('📊', 'Session Results'), ('📈', 'Mode Statistics'), ('⚙️', 'Settings')]

    def __init__(self, settings:dict, tray_ref=None):
        super().__init__()
        self._tray=tray_ref; self._settings=dict(settings)
        self.setWindowTitle('🐦  Canary')
        self.setMinimumSize(900,660); self.resize(1000,720)
        self.setStyleSheet(f'background:{C["bg"]};color:{C["text"]};')

        cw=QWidget(); cw.setStyleSheet(f'background:{C["bg"]};')
        self.setCentralWidget(cw)
        root=QVBoxLayout(cw); root.setContentsMargins(0,0,0,0); root.setSpacing(0)

        # Header
        hbar=QFrame(); hbar.setFixedHeight(58)
        hbar.setStyleSheet(f'QFrame{{background:{C["card"]};border-bottom:1px solid {C["border"]}}}')
        hl=QHBoxLayout(hbar); hl.setContentsMargins(24,0,24,0)
        lr=QHBoxLayout(); lr.setSpacing(10)
        logo=QLabel('🐦'); logo.setStyleSheet('font-size:22pt;background:transparent;')
        lr.addWidget(logo)
        name_lbl=QLabel('Canary')
        name_lbl.setStyleSheet(f'color:{C["purple"]};font-size:15pt;'
                                f'font-weight:bold;background:transparent;')
        lr.addWidget(name_lbl)
        hl.addLayout(lr); hl.addStretch()

        self._status_badge=QLabel('⬤  Inactive')
        self._status_badge.setStyleSheet(f"""QLabel{{color:{C['muted']};background:{C['card2']};
            border:1px solid {C['border']};border-radius:12px;padding:4px 14px;font-size:10pt;}}""")
        hl.addWidget(self._status_badge); hl.addSpacing(12)

        self._sess_btn=QPushButton('▶  Start Session')
        self._sess_btn.setFixedHeight(36); self._sess_btn.setCursor(Qt.PointingHandCursor)
        self._set_btn_start(); self._sess_btn.clicked.connect(self._toggle)
        hl.addWidget(self._sess_btn); root.addWidget(hbar)

        # Calibration bar
        self._calib_bar=QProgressBar()
        self._calib_bar.setRange(0,100); self._calib_bar.setValue(0)
        self._calib_bar.setFixedHeight(6); self._calib_bar.setTextVisible(False)
        self._calib_bar.setStyleSheet("""
            QProgressBar{background:#1e1b2e;border:none;}
            QProgressBar::chunk{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 #4c1d95,stop:0.5 #8b5cf6,stop:1 #c084fc);}""")
        self._calib_bar.hide(); root.addWidget(self._calib_bar)

        # Tabs
        tbar=QFrame(); tbar.setFixedHeight(42)
        tbar.setStyleSheet(f'QFrame{{background:{C["card"]};border-bottom:1px solid {C["border"]}}}')
        tl=QHBoxLayout(tbar); tl.setContentsMargins(16,0,16,0); tl.setSpacing(0)
        self._tabs=[]
        for i,(ico,txt) in enumerate(self.TABS):
            btn=QPushButton(f'{ico}  {txt}')
            btn.setCheckable(True); btn.setChecked(i==0)
            btn.setFixedHeight(42); btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _,idx=i: self._switch_tab(idx))
            self._tabs.append(btn); tl.addWidget(btn)
        tl.addStretch(); self._style_tabs(); root.addWidget(tbar)

        self._stack=QStackedWidget(); self._stack.setStyleSheet(f'background:{C["bg"]};')
        self._live       = LiveDashboardPage()
        self._results    = SessionResultsPage()
        self._mode_stats = ModeStatsPage()
        self._settings_page = SettingsPage(self._settings)
        self._stack.addWidget(self._live)         # 0
        self._stack.addWidget(self._results)      # 1
        self._stack.addWidget(self._mode_stats)   # 2
        self._stack.addWidget(self._settings_page)  # 3
        root.addWidget(self._stack)

        self._results.clear_history_requested.connect(self._on_clear_history)
        self._settings_page.settings_changed.connect(self._on_settings)
        self._session_active=False
        self._calib_timer=QTimer(self); self._calib_timer.timeout.connect(self._update_calib)

        # Live update timer (every 1 s while session active)
        self._live_timer=QTimer(self); self._live_timer.timeout.connect(self._update_live)

    def _style_tabs(self):
        for i,btn in enumerate(self._tabs):
            if btn.isChecked():
                btn.setStyleSheet(f"""QPushButton{{background:transparent;color:{C['purple']};
                    border:none;border-bottom:2.5px solid {C['purple']};
                    padding:0 20px;font-size:11pt;font-weight:bold;}}""")
            else:
                btn.setStyleSheet(f"""QPushButton{{background:transparent;color:{C['muted']};
                    border:none;border-bottom:2.5px solid transparent;
                    padding:0 20px;font-size:11pt;}}
                    QPushButton:hover{{color:{C['text']};}}""")

    def _switch_tab(self,idx):
        for i,b in enumerate(self._tabs): b.setChecked(i==idx)
        self._style_tabs(); self._stack.setCurrentIndex(idx)
        if idx == 2:  # Mode Statistics — pull fresh data each time it's opened
            self._mode_stats.refresh()

    def _set_btn_start(self):
        self._sess_btn.setStyleSheet("""QPushButton{
            background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 #4c1d95,stop:0.4 #7c3aed,stop:1 #a855f7);
            color:white;border:none;border-radius:10px;
            padding:0 22px;font-size:11pt;font-weight:bold;
            border:1px solid #a855f7;}
            QPushButton:hover{background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 #6d28d9,stop:1 #c084fc);}""")
        self._sess_btn.setText('▶  Start Session')

    def _set_btn_stop(self):
        self._sess_btn.setStyleSheet(f"""QPushButton{{
            background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 {C['red_d']},stop:1 {C['red']});
            color:white;border:none;border-radius:10px;
            padding:0 22px;font-size:11pt;font-weight:bold;}}
            QPushButton:hover{{background:{C['red']};}}""")
        self._sess_btn.setText('⏹  End Session')

    def _toggle(self):
        if self._session_active: self._tray.stop_session() if self._tray else None
        else:                    self._tray.start_session() if self._tray else None

    def on_session_started(self, history: List[dict]):
        self._session_active=True; self._set_btn_stop()
        self._status_badge.setText('⬤  Active')
        self._status_badge.setStyleSheet(f"""QLabel{{color:{C['green']};background:{C['green']}22;
            border:1px solid {C['green']}66;border-radius:12px;padding:4px 14px;font-size:10pt;}}""")
        self._calib_bar.show(); self._calib_timer.start(200)
        self._live.set_active(time.time(), history)
        self._live_timer.start(1000)
        self._switch_tab(0)

    def on_session_ended(self,sd:SessionData,history:List[dict]):
        self._session_active=False; self._set_btn_start()
        self._status_badge.setText('⬤  Inactive')
        self._status_badge.setStyleSheet(f"""QLabel{{color:{C['muted']};background:{C['card2']};
            border:1px solid {C['border']};border-radius:12px;padding:4px 14px;font-size:10pt;}}""")
        self._calib_bar.hide(); self._calib_timer.stop()
        self._live_timer.stop()
        self._live.set_inactive()
        self._results.load(sd,history); self._switch_tab(1)

    def _update_calib(self):
        if self._tray and self._tray.detector:
            pct=self._tray.detector.calib_progress()
            self._calib_bar.setValue(int(pct))
            if pct>=100: self._calib_bar.hide(); self._calib_timer.stop()

    def _update_live(self):
        if self._tray and self._tray.detector:
            live = self._tray.detector.get_live()
            self._live.update_live(live)

    def add_live_event(self, ts: float, kind: str, detail: str):
        self._live.add_event(ts, kind, detail)

    def _on_clear_history(self):
        if not self._tray:
            return
        remaining = self._tray.clear_session_history()
        # The just-completed session's own report (sd) has no historical
        # backing left to compare against now — show the placeholder rather
        # than a comparison/trend view built on data that no longer exists.
        # This never touches a session currently in progress: that lives
        # only in the active detector's in-memory SessionData, untouched by
        # clear_session_history().
        self._results._show_placeholder()

    def _on_settings(self,s:dict):
        self._settings=dict(s); _save_settings(s)
        if self._tray:
            self._tray.overlay.set_voice(s['voice_enabled'],s['voice_gender'])
            self._tray._settings=dict(s)

    def get_settings(self)->dict:
        return self._settings_page.get_settings()

    def closeEvent(self,ev): ev.ignore(); self.hide()


# ══════════════════════════════════════════════════════════════════════════════
#  Canary tray icon
# ══════════════════════════════════════════════════════════════════════════════

def _make_canary_icon(state:str='idle')->QIcon:
    col_map={'idle':'#64748b','good':'#10b981','posture':'#ef4444',
             'stress':'#f59e0b','water':'#3b82f6','camera':'#f97316'}
    col=QColor(col_map.get(state,'#fbbf24'))
    px=QPixmap(64,64); px.fill(Qt.transparent)
    p=QPainter(px); p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(col)); p.setPen(Qt.NoPen)
    p.drawEllipse(14,24,34,28)
    p.drawEllipse(28,10,22,20)
    beak=QPainterPath()
    beak.moveTo(50,18); beak.lineTo(60,21); beak.lineTo(50,24)
    beak.closeSubpath(); p.fillPath(beak,QBrush(col.darker(130)))
    wing=QPainterPath()
    wing.moveTo(18,30); wing.cubicTo(8,22,4,38,18,42)
    wing.closeSubpath(); p.fillPath(wing,QBrush(col.darker(115)))
    tail=QPainterPath()
    tail.moveTo(14,44); tail.lineTo(4,52); tail.lineTo(10,40)
    tail.closeSubpath(); p.fillPath(tail,QBrush(col.darker(120)))
    p.setBrush(QBrush(QColor('#1a0a00'))); p.drawEllipse(40,15,5,5)
    pen=QPen(col.darker(140),2); p.setPen(pen)
    p.drawLine(26,52,22,62); p.drawLine(34,52,30,62)
    p.drawLine(22,62,18,62); p.drawLine(30,62,26,62)
    p.end(); return QIcon(px)


# ══════════════════════════════════════════════════════════════════════════════
#  TrayApp
# ══════════════════════════════════════════════════════════════════════════════

class TrayApp(QObject):
    def __init__(self,app:QApplication):
        super().__init__()
        self._app=app
        self.detector:Optional[StressPostureDetector]=None
        self._settings=_load_settings()

        self.overlay=NotificationOverlay()
        self.overlay.start()
        self.overlay.set_voice(self._settings['voice_enabled'],
                               self._settings['voice_gender'])

        history_db.init_db()
        self._history=_load_history()

        # ── Alert state tracking for change-only alerts ───────────────────────
        # We track the PREVIOUS alert state; only fire when it changes
        self._prev_posture_alerting = False  # was posture alert active last check?
        self._prev_stress_alerting  = False
        self._last_camera_alert_t   = 0.0
        self._no_detect_since: Optional[float] = None  # first time face went missing
        self._camera_ok = True

        # ── Horizon Mode (20-20-20) state ─────────────────────────────────────
        # _horizon_active: True for the whole time the calm eye-rest screen is
        #   up (from the moment HorizonMode.start() fires to its finished
        #   signal). While True, _poll_detector and _on_alert both suppress
        #   every OTHER notification kind — only the "please look away" nudge
        #   is allowed to fire, per the requirement that Horizon Mode shows no
        #   other alert type while it's active.
        # _horizon_owns_detector: True only when Horizon Mode itself started
        #   the detector (i.e. triggered from the tray with no session
        #   running). In that case we tear the detector back down when
        #   Horizon Mode finishes, since the user never asked for an ongoing
        #   monitored session — just a one-off break. If a real session is
        #   already active, this stays False and stop_session() (not us)
        #   owns the detector's lifecycle.
        self._horizon_active        = False
        self._horizon_owns_detector = False
        self._horizon_owns_poll_timer = False
        self._last_horizon_alert_t  = 0.0
        self._horizon_gaze          = None  # HorizonGazeMonitor, only while the calm screen is up

        # Recurring 20-minute Horizon Mode trigger, per the 20-20-20 rule.
        # Only runs while a real session is active — started in
        # start_session(), stopped in stop_session(). Manually triggering
        # Horizon Mode from the tray (trigger_horizon_mode) is completely
        # separate and does not start/need this timer.
        self._horizon_timer = QTimer(self)
        self._horizon_timer.timeout.connect(self.trigger_horizon_mode)

        # ── Calm Mode (Phase 1: manual-only) state ────────────────────────────
        # _calm_active: True for the whole time the breathing screen is up.
        # Guards against a second tray click opening a duplicate window —
        # same pattern as _horizon_active above. Calm Mode does not touch
        # the detector, the poll timer, or session state at all; it's a
        # pure visual overlay with no automatic trigger in this phase.
        self._calm_active = False
        self._calm_mode = None

        # ── Centralized mode state (Phase 3) ──────────────────────────────────
        # Single source of truth for "is an intervention/recovery in progress".
        # One of: IDLE, HORIZON, CALM, UNPLUG, RECOVERY_WAIT.
        # _horizon_active/_calm_active above are kept as-is (other code reads
        # them) but are always kept in sync with this.
        self._active_mode = 'IDLE'
        self._session_running = False

        # Automatic Calm (sustained high stress) — hysteresis state
        self._calm_high_stress_since: Optional[float] = None
        self._calm_recovered_since:   Optional[float] = None
        self._calm_rearmed = True
        self._calm_for_unplug = False  # True only when this Calm run is stage 1 of an automatic Unplug

        # Automatic Unplug (meaningful-alert threshold) — accumulator + cooldown
        self._meaningful_alert_count = 0
        self._unplug_cooldown_until  = 0.0
        self._unplug_is_automatic    = False
        self._unplug_using_shared_detector = False
        # Set by stop_session() when Unplug is mid-run on the session's
        # shared detector at the moment the session is stopped — holds the
        # detector object whose teardown + session bookkeeping is deferred
        # until Unplug's own _on_closing_done fires (see trigger_unplug_mode
        # and stop_session/_finish_stopping_session).
        self._pending_session_detector = None

        # ── Phase 4: per-session Horizon/Calm/Unplug usage + persistent
        # mode-usage logging. Reset in start_session(); read once, at
        # session end, by _finish_stopping_session() to build the DB row.
        # Cumulative meaningful-alert TOTAL for history/statistics — unlike
        # the operational self._meaningful_alert_count above, this one is
        # never reset mid-session (Calm-resolve/Unplug-complete reset the
        # operational counter, not this one), so it reflects everything
        # that happened in the session, for the record.
        self._session_meaningful_alert_total = 0
        self._session_horizon_uses = 0
        self._session_horizon_secs = 0.0
        self._session_calm_uses = 0
        self._session_calm_secs = 0.0
        self._session_unplug_uses = 0
        self._session_unplug_secs = 0.0
        self._session_unplug_exercises_completed = 0
        # Per-run bookkeeping, set when each mode is triggered and read
        # when it finishes (see trigger_*/​_on_*_finished below).
        self._horizon_run_start = 0.0
        self._horizon_run_during_session = False
        self._calm_run_start = 0.0
        self._calm_run_during_session = False
        self._unplug_run_start = 0.0
        self._unplug_run_during_session = False

        # Calm → recovery → Unplug reassessment timer
        self._recovery_timer = QTimer(self)
        self._recovery_timer.setSingleShot(True)
        self._recovery_timer.timeout.connect(self._on_recovery_timeout)

        self._tray_state='idle'

        self.dashboard=DashboardWindow(self._settings,tray_ref=self)
        self.dashboard.hide()

        # Tray icon
        self._icon=QSystemTrayIcon(_make_canary_icon('idle'),self._app)
        self._icon.setToolTip('Canary 🐦')

        menu=QMenu()
        menu.setStyleSheet(f"""
            QMenu{{background:{C['card']};color:{C['text']};
            border:1px solid {C['border']};border-radius:8px;padding:4px;}}
            QMenu::item{{padding:8px 24px;border-radius:6px;}}
            QMenu::item:selected{{background:{C['canary']}44;color:{C['canary']};}}
            QMenu::separator{{height:1px;background:{C['border']};margin:4px 8px;}}""")

        self._start_act=QAction('▶  Start Session',self._app)
        self._start_act.triggered.connect(self.start_session); menu.addAction(self._start_act)

        self._stop_act=QAction('⏹  End Session',self._app)
        self._stop_act.setEnabled(False)
        self._stop_act.triggered.connect(self.stop_session); menu.addAction(self._stop_act)

        menu.addSeparator()
        dash_act=QAction('📊  Open Dashboard',self._app)
        dash_act.triggered.connect(self.show_dashboard); menu.addAction(dash_act)

        menu.addSeparator()
        unplug_act=QAction('🔌  Trigger Unplug Mode',self._app)
        unplug_act.triggered.connect(self.trigger_unplug_mode); menu.addAction(unplug_act)

        horizon_act=QAction('👁  Trigger Horizon Mode',self._app)
        horizon_act.triggered.connect(self.trigger_horizon_mode); menu.addAction(horizon_act)

        calm_act=QAction('🌊  Calm Mode',self._app)
        calm_act.triggered.connect(self.trigger_calm_mode); menu.addAction(calm_act)

        menu.addSeparator()
        quit_act=QAction('✕  Quit Canary',self._app)
        quit_act.triggered.connect(self._quit); menu.addAction(quit_act)

        self._icon.setContextMenu(menu)
        self._icon.activated.connect(self._on_activated)
        self._icon.show()
        self._icon.showMessage('Canary 🐦',
            'Running in tray. Left-click to open Dashboard.',
            QSystemTrayIcon.Information,4000)

        # Polling timer — check detector state every 500 ms for change-based alerts
        self._poll_timer=QTimer(self)
        self._poll_timer.timeout.connect(self._poll_detector)

    # ── Navigation ────────────────────────────────────────────────────────────

    def _on_activated(self,reason):
        if reason in(QSystemTrayIcon.Trigger,QSystemTrayIcon.DoubleClick):
            self.show_dashboard()

    def show_dashboard(self):
        self.dashboard.show(); self.dashboard.raise_()
        self.dashboard.activateWindow()

    # ── Unplug / Horizon Mode triggers ──────────────────────────────────────────

    def _mode_busy(self) -> bool:
        """
        True while ANY intervention or the post-Calm recovery decision is in
        progress. Every manual and automatic trigger re-checks this
        immediately before doing anything else, so a stale QTimer callback
        or a manual click during another mode is a safe no-op rather than a
        second overlapping window.
        """
        return self._active_mode != 'IDLE'

    def _record_mode_run(self, mode: str, start_ts: float, end_ts: float,
                          during_session: bool, completed: bool,
                          extra: Optional[dict] = None):
        """
        Phase 4: log one Horizon/Calm/Unplug run to the mode_usage table
        (Mode Statistics reads this — every run, session or standalone) and,
        if it happened during an active monitoring session, fold it into
        this session's running totals (the sessions table reads THOSE, at
        session end). during_session is passed in rather than re-checked
        here because self._session_running may have already changed
        between when the run started and when it finished.
        """
        history_db.insert_mode_usage(mode, start_ts, end_ts, during_session,
                                      completed, extra)
        if not during_session:
            return
        duration = max(end_ts - start_ts, 0.0)
        if mode == 'horizon':
            self._session_horizon_uses += 1
            self._session_horizon_secs += duration
        elif mode == 'calm':
            self._session_calm_uses += 1
            self._session_calm_secs += duration
        elif mode == 'unplug':
            self._session_unplug_uses += 1
            self._session_unplug_secs += duration
            if extra:
                self._session_unplug_exercises_completed += extra.get('exercises_completed', 0)

    def trigger_unplug_mode(self, automatic: bool = False):
        """
        Trigger Unplug Mode: Opening Animation -> 3D wellness
        environment (the 7-exercise sequence) -> Closing Animation.

        Each handoff below shows the NEXT full-screen window (already
        raised on top) BEFORE dismissing the current one, rather than
        the other way around. Both windows involved at each handoff are
        full-screen and solid black/opaque, so as long as the new one
        is on top first, hiding the old one afterwards can only reveal
        the new one underneath — never the desktop. Previously each
        stage hid itself immediately before signalling "done", which
        left a real gap (however brief) where the desktop showed
        through while the next stage was still being constructed —
        worse the longer that setup took (e.g. ExerciseEnvironment's
        camera/coordinator startup). See OpeningAnimation.dismiss() /
        ExerciseEnvironment.dismiss() for the same reasoning at the
        single-widget level.

        show()/raise_() alone only SCHEDULE a paint — they don't force
        one to actually happen before the next line of code runs, so
        even with the ordering above, the old window's dismiss() could
        still execute before the new window's first frame has really
        been composited (a real, if usually brief, source of exactly
        the flash this ordering is meant to prevent). processEvents()
        forces Qt to flush that pending paint synchronously, and the
        small 60ms delay before dismiss() gives the OS-level compositor
        a little real wall-clock time on top of that for Chromium's own
        (separate-process) compositor in ExerciseEnvironment's
        QWebEngineView specifically, which processEvents() alone can't
        force to sync.

        `automatic=True` is passed only by the internal Phase 3 orchestration
        (the alert-threshold path, directly or via the Calm→recovery chain);
        it starts the post-completion cooldown and resets the meaningful-alert
        counter. Manual tray triggers leave both untouched, per the existing
        "demo trigger" behavior.
        """
        if self._mode_busy():
            return  # another intervention (or the recovery decision) owns the screen
        self._active_mode = 'UNPLUG'
        self._unplug_is_automatic = automatic
        self._unplug_run_start = time.time()
        self._unplug_run_during_session = self._session_running
        # Snapshot now: ExerciseEnvironment below is handed self.detector
        # (if any) as shared_detector=. Whether THIS run is borrowing the
        # session's detector — and therefore must not have it stopped out
        # from under it — is fixed at trigger time, not re-derived later
        # (self.detector may already be None by the time stop_session()
        # runs, per the deferred-teardown logic there).
        self._unplug_using_shared_detector = self.detector is not None

        try:
            from ui.unplug.opening_animation import OpeningAnimation
            from ui.unplug.exercise_environment import ExerciseEnvironment
            from ui.unplug.closing_animation import ClosingAnimation

            character = self._settings.get('unplug_gender', 'boy')

            def _on_closing_done():
                # Main interface is already what's underneath — nothing
                # further to do once the closing animation's black
                # screen fades away. This is the one stage in the chain
                # where revealing the desktop IS the correct final
                # result, so ClosingAnimation hides itself as before.
                self._active_mode = 'IDLE'
                if self._unplug_is_automatic:
                    self._unplug_cooldown_until = time.time() + UNPLUG_COOLDOWN
                    self._meaningful_alert_count = 0
                self._unplug_is_automatic = False
                self._unplug_using_shared_detector = False

                # Phase 4: log this run (Mode Statistics + this session's
                # totals). last_session_log is UnplugSessionLog — populated
                # by ExerciseValidationCoordinator.finish_session() whenever
                # the exercise routine completed normally; stays None if the
                # window was closed before any exercise routine ran.
                log = getattr(self._unplug_environment, 'last_session_log', None)
                extra = None
                unplug_completed = True
                if log is not None:
                    unplug_completed = bool(log.completed)
                    extra = {
                        'exercises_completed': len(log.entries),
                        'cv_validated_count':  log.cv_validated_count,
                        'character':           log.character,
                    }
                self._record_mode_run('unplug', self._unplug_run_start, time.time(),
                                       self._unplug_run_during_session,
                                       completed=unplug_completed, extra=extra)

                # If stop_session() was called while this Unplug run was
                # still using the session's detector, it deferred the
                # actual teardown to right here (see stop_session) rather
                # than stopping the camera out from under
                # ExerciseValidationCoordinator mid-exercise. By this
                # point the coordinator has already released the shared
                # detector's frame_hook (ExerciseEnvironment._on_session_
                # complete/closeEvent both call coordinator.stop() before
                # animation_done fires, which is what got us here), so
                # it's now safe to actually stop it and finish the
                # session bookkeeping that was put on hold.
                pending = self._pending_session_detector
                if pending is not None:
                    self._pending_session_detector = None
                    self._finish_stopping_session(pending)

            def _on_exercises_done():
                closing = ClosingAnimation()
                self._unplug_closing = closing
                closing.animation_done.connect(_on_closing_done)
                closing.start()                       # shown + raised on top first...
                QApplication.processEvents()          # ...force that paint to actually flush...
                QTimer.singleShot(60, self._unplug_environment.dismiss)  # ...then hide the environment

            def _on_opening_done():
                # Share the already-running detector (if any) so Unplug
                # Mode's camera-based exercise validation reuses the
                # SAME camera device rather than opening a second one —
                # same pattern trigger_horizon_mode uses below. If no
                # monitoring session is active, ExerciseEnvironment's
                # coordinator creates and owns a temporary detector for
                # just this Unplug session.
                env = ExerciseEnvironment(character=character, shared_detector=self.detector)
                self._unplug_environment = env
                env.animation_done.connect(_on_exercises_done)
                env.start()                           # shown + raised on top first...
                QApplication.processEvents()          # ...force that paint to actually flush...
                QTimer.singleShot(5, self._unplug_anim.dismiss)   # ...then hide the opening animation

            anim = OpeningAnimation()
            self._unplug_anim = anim
            anim.animation_done.connect(_on_opening_done)
            anim.start()
        except Exception as e:
            print(f'[Canary] Could not start Unplug Mode: {e}')
            self._active_mode = 'IDLE'
            self._unplug_is_automatic = False
            self._unplug_using_shared_detector = False

    def trigger_horizon_mode(self):
        """
        Trigger Horizon Mode: plays the brief HORIZON logo intro first,
        then hands off into the calm 20-20-20 eye-rest overlay once the
        intro settles into stillness.

        Works two ways:
          - Automatically, every 20 minutes while a real session is
            active (see the _horizon_timer wired up in __init__/
            start_session/stop_session) — the 20-20-20 rule.
          - Manually from the tray menu at any time, session or no
            session. If no session is running, this starts a detector
            just for the duration of the break (so the "please look
            away" camera check has something to read from), and tears
            it back down again afterwards — it does NOT start a real
            monitored session.
        """
        if self._mode_busy():
            return  # another intervention (or recovery decision) owns the screen —
                     # the recurring 20-min timer will simply try again next interval
        # Claim the mode slot immediately (not just once _show_calm_screen
        # runs) — the logo intro plays first, and a second trigger arriving
        # during that intro must not be allowed to start a second overlay.
        self._active_mode = 'HORIZON'
        self._horizon_run_start = time.time()
        self._horizon_run_during_session = self._session_running

        try:
            from ui.horizon.logo_intro import LogoIntro
            from ui.horizon.horizon_mode import HorizonMode
            from ui.horizon.horizon_gaze import HorizonGazeMonitor

            # If nothing is watching the camera right now, Horizon Mode
            # needs its own detector for the look-away check — started
            # here, and it's ours to stop again when the break ends.
            # _poll_detector (where the look-away check itself lives) only
            # runs while _poll_timer is active, and that timer is normally
            # only started by start_session() — so a standalone tray
            # trigger needs to start it here too, or the check never fires.
            self._horizon_owns_detector = False
            self._horizon_owns_poll_timer = False
            if not self.detector:
                self.detector = StressPostureDetector(on_alert=self._on_alert)
                self.detector.start()
                self._horizon_owns_detector = True
            if not self._poll_timer.isActive():
                self._poll_timer.start(500)
                self._horizon_owns_poll_timer = True

            def _show_calm_screen():
                self._horizon_active = True
                self._active_mode = 'HORIZON'
                self._last_horizon_alert_t = 0.0

                # Real gaze/head-pose monitoring, not a crude proxy —
                # see ui/horizon/horizon_gaze.py. Attached only now
                # (not any earlier) and only for the lifetime of this
                # calm screen. Frames arrive via the detector's
                # frame_hook at real camera framerate (~7fps), not the
                # slow 500ms UI poll, so the monitor's own 0.75s
                # debounce is actually meaningful.
                self._horizon_gaze = HorizonGazeMonitor()
                if self.detector:
                    self.detector.frame_hook = self._horizon_gaze.process_frame

                mode = HorizonMode()
                self._horizon_mode = mode
                mode.finished.connect(self._on_horizon_finished)
                mode.start()

            intro = LogoIntro()
            self._horizon_intro = intro
            intro.finished.connect(_show_calm_screen)
            intro.start()
        except Exception as e:
            print(f'[Canary] Could not start Horizon Mode: {e}')
            self._horizon_active = False
            self._active_mode = 'IDLE'

    def trigger_calm_mode(self, for_unplug: bool = False):
        """
        Trigger Calm Mode: a short ocean-blue breathing particle
        animation (INHALE/HOLD/EXHALE, a few cycles, ending in a
        particle smile that fades away). Does not touch the
        detector/camera.

        Three entry paths, per Phase 3:
          - Manual tray trigger (`for_unplug=False`, the default).
          - Automatic sustained-high-stress trigger (`for_unplug=False`)
            — a Calm that fires this way is a self-contained wellness
            nudge and must NOT lead into Unplug afterward.
          - Stage 1 of an automatic Unplug (`for_unplug=True`) — only
            _on_meaningful_alert() passes this. In that case, finishing
            Calm moves into RECOVERY_WAIT rather than back to IDLE.
        """
        if self._mode_busy():
            return  # already showing another intervention; don't stack

        try:
            from ui.calm.calm_mode import CalmMode
        except Exception as e:
            print(f'[Canary] Could not start Calm Mode: {e}')
            return

        self._active_mode = 'CALM'
        self._calm_active = True
        self._calm_for_unplug = for_unplug
        self._calm_run_start = time.time()
        self._calm_run_during_session = self._session_running
        mode = CalmMode()
        self._calm_mode = mode
        mode.finished.connect(self._on_calm_finished)
        mode.start()

    def _on_calm_finished(self):
        self._calm_active = False
        self._calm_mode = None
        was_for_unplug = self._calm_for_unplug
        self._calm_for_unplug = False
        self._record_mode_run('calm', self._calm_run_start, time.time(),
                               self._calm_run_during_session, completed=True,
                               extra={'for_unplug': was_for_unplug})

        if was_for_unplug:
            # Stage 1 of the automatic Unplug chain: don't decide yet.
            # Wait CALM_UNPLUG_RECOVERY_INTERVAL, then reassess CURRENT
            # sustained stress (not the value that originally triggered
            # Calm) before deciding whether Unplug is still warranted.
            self._active_mode = 'RECOVERY_WAIT'
            self._recovery_timer.start(int(CALM_UNPLUG_RECOVERY_INTERVAL * 1000))
        else:
            self._active_mode = 'IDLE'

    def _on_recovery_timeout(self):
        """
        Fires CALM_UNPLUG_RECOVERY_INTERVAL after an alert-triggered Calm
        completes. Re-checks session/mode state first (stale-callback
        guard), then reassesses sustained stress via the detector's
        smoothed live score — never a single instantaneous frame.
        """
        if self._active_mode != 'RECOVERY_WAIT':
            return  # state moved on (e.g. session ended) — stale callback, ignore

        if not self.detector or not self._session_running:
            # Session ended while we were waiting — don't launch anything.
            self._active_mode = 'IDLE'
            self._meaningful_alert_count = 0
            return

        live   = self.detector.get_live()
        stress = live.get('stress_score', 0.0)

        # Clear RECOVERY_WAIT before deciding: trigger_unplug_mode's own
        # _mode_busy() guard requires IDLE, and if stress has recovered we
        # want to land in IDLE either way.
        self._active_mode = 'IDLE'

        if stress >= POST_CALM_STILL_ELEVATED_LEVEL:
            # Still significantly elevated (or it rose again) — Unplug.
            self.trigger_unplug_mode(automatic=True)
        else:
            # Sufficiently reduced/stable — cancel the pending Unplug and
            # don't let the alerts that led here cause another one soon.
            self._meaningful_alert_count = 0

    def _on_horizon_finished(self):
        """Runs once the calm eye-rest screen closes, however it closed."""
        self._horizon_active = False
        self._active_mode = 'IDLE'
        self._record_mode_run('horizon', self._horizon_run_start, time.time(),
                               self._horizon_run_during_session, completed=True)
        # If the user exited while the gaze notice was up (they were
        # still looking at the screen), it must vanish right along with
        # the calm screen — _poll_detector won't touch it again now that
        # _horizon_active is False.
        self.overlay.set_horizon_active(False)
        if self.detector:
            self.detector.frame_hook = None
        if getattr(self, '_horizon_gaze', None) is not None:
            self._horizon_gaze.close()
            self._horizon_gaze = None
        if self._horizon_owns_detector and self.detector:
            self.detector.stop()
            self.detector = None
        if getattr(self, '_horizon_owns_poll_timer', False):
            self._poll_timer.stop()
        self._horizon_owns_detector = False
        self._horizon_owns_poll_timer = False

    # ── Session ───────────────────────────────────────────────────────────────

    def start_session(self):
        if self.detector and self.detector._running:
            if self._horizon_owns_detector:
                # A Horizon Mode break already spun up a detector of its
                # own (no session was active at the time). The user is
                # now explicitly starting a real session — adopt that
                # same detector rather than leaving it to Horizon Mode's
                # cleanup, which would otherwise stop it out from under
                # the session the moment the break screen closes.
                self._horizon_owns_detector = False
            else:
                return
        else:
            self.detector=StressPostureDetector(on_alert=self._on_alert)
            self.detector.start()
        self._start_act.setEnabled(False); self._stop_act.setEnabled(True)
        # Reset change-tracking state
        self._prev_posture_alerting = False
        self._prev_stress_alerting  = False
        self._no_detect_since       = None
        self._camera_ok             = True
        self._last_camera_alert_t   = 0.0
        self._session_running       = True
        # Phase 3: reset per-session automatic-trigger state so a previous
        # session's alerts/stress history can't cause an immediate
        # intervention in this new one.
        self._meaningful_alert_count  = 0
        self._calm_high_stress_since  = None
        self._calm_recovered_since    = None
        self._calm_rearmed            = True
        # Phase 4: reset this session's usage/alert totals for the DB row
        # that will be written when this session ends.
        self._session_meaningful_alert_total     = 0
        self._session_horizon_uses               = 0
        self._session_horizon_secs               = 0.0
        self._session_calm_uses                  = 0
        self._session_calm_secs                  = 0.0
        self._session_unplug_uses                = 0
        self._session_unplug_secs                = 0.0
        self._session_unplug_exercises_completed = 0
        self._poll_timer.start(500)
        if self._settings.get('horizon_enabled', True):
            self._horizon_timer.start(HORIZON_INTERVAL_MS)  # 20-20-20 rule for this session
        self.dashboard.on_session_started(list(self._history))
        self._tray_state='good'; self._icon.setIcon(_make_canary_icon('good'))
        self._icon.showMessage('Canary 🐦 — Session Started',
            'Sit naturally for ~30 s to calibrate.',
            QSystemTrayIcon.Information,5000)

    def stop_session(self):
        if not self.detector: return
        self._poll_timer.stop()
        self._horizon_timer.stop()
        self._session_running = False
        # Cancel any pending automatic-intervention callback so it cannot
        # fire (and launch a mode) after the session has ended. If we were
        # mid-recovery-wait with no mode window actually open, drop back to
        # IDLE now rather than leaving the state machine stuck.
        self._recovery_timer.stop()
        if self._active_mode == 'RECOVERY_WAIT':
            self._active_mode = 'IDLE'
            self._meaningful_alert_count = 0

        detector = self.detector

        if self._active_mode == 'UNPLUG' and self._unplug_using_shared_detector:
            # Unplug's ExerciseEnvironment/ExerciseValidationCoordinator is
            # currently reading frames from THIS detector object via
            # shared_detector= (see trigger_unplug_mode) — it does NOT own
            # it and will not stop it itself. Calling detector.stop() here
            # would release the camera out from under a still-running
            # exercise sequence (frozen preview / progress dots that can
            # never fill), so defer the actual stop + session bookkeeping
            # until Unplug's own flow finishes naturally
            # (_on_closing_done). We deliberately do NOT touch
            # _active_mode here — Unplug's existing standalone cleanup
            # still owns that. "Start Session" stays disabled in the
            # meantime too — a second detector opening the camera while
            # this one is still in use by Unplug would just contend for
            # the same hardware.
            self.detector = None  # tray/dashboard read this as "no active session" immediately
            self._pending_session_detector = detector
            self._stop_act.setEnabled(False)
            self._tray_state = 'idle'; self._icon.setIcon(_make_canary_icon('idle'))
            self._icon.showMessage('Canary 🐦 — Session Ending',
                'Finishing your current Unplug break first…',
                QSystemTrayIcon.Information, 4000)
            return

        self._start_act.setEnabled(True); self._stop_act.setEnabled(False)
        self.detector = None
        self._finish_stopping_session(detector)

    def _finish_stopping_session(self, detector: StressPostureDetector):
        """
        The actual detector teardown + session-end bookkeeping that used to
        be inline in stop_session(). Split out so it can also run later,
        deferred, when stop_session() is called mid-Unplug on the shared
        detector (see there).
        """
        sd = detector.stop()
        extra = {
            'meaningful_alerts':          self._session_meaningful_alert_total,
            'horizon_uses':               self._session_horizon_uses,
            'horizon_secs':               round(self._session_horizon_secs, 1),
            'calm_uses':                  self._session_calm_uses,
            'calm_secs':                  round(self._session_calm_secs, 1),
            'unplug_uses':                self._session_unplug_uses,
            'unplug_secs':                round(self._session_unplug_secs, 1),
            'unplug_exercises_completed': self._session_unplug_exercises_completed,
        }
        row = _session_to_dict(sd, extra)
        history_db.insert_session(row)
        self._history.append(row)
        if len(self._history) > HISTORY_MEMORY_CAP:
            self._history = self._history[-HISTORY_MEMORY_CAP:]
        self.dashboard.on_session_ended(sd, self._history)
        self.show_dashboard()
        self._start_act.setEnabled(True)
        self._tray_state='idle'; self._icon.setIcon(_make_canary_icon('idle'))
        self._icon.showMessage('Canary 🐦 — Session Ended',
            f'Duration: {sd.duration_min} min — see your report in the Dashboard.',
            QSystemTrayIcon.Information,4000)

    def clear_session_history(self):
        """
        Phase 4: wipe stored session HISTORY only (the `sessions` table) —
        never mode_usage (Mode Statistics has no clear action) and never
        anything about a session currently in progress, which lives only
        in the active detector's in-memory SessionData until it's stopped
        and inserted, and is therefore untouched by this.
        """
        if history_db.clear_sessions():
            self._history = []
        return list(self._history)

    # ── Change-based alert polling ────────────────────────────────────────────

    def _poll_detector(self):
        """
        Called every 500 ms from the main thread.
        ONLY responsible for:
          1. Updating the tray icon colour to reflect current state
          2. Triggering the camera-blocked alert (which the detector cannot do)
          3. Keeping the live dashboard gauges up to date

        Overlay notifications are EXCLUSIVELY fired by _on_alert(), which the
        detector calls only after a signal is sustained for SUSTAINED_SEC AND
        the per-kind cooldown has passed.  _poll_detector never calls
        overlay.push() — that would cause double popups.
        """
        if not self.detector:
            return

        # ── Horizon Mode branch ────────────────────────────────────────────
        # While the calm eye-rest screen is up, this is ALL _poll_detector
        # does: check the debounced state from HorizonGazeMonitor (real
        # eyes-open + gaze-direction + head-pose evaluation, sustained for
        # TRANSITION_SEC before it's trusted — see horizon_gaze.py) and
        # continuously reflect it via set_horizon_active(): shows the
        # instant looking_at_screen goes True, hides the instant it goes
        # False — no cooldown, no fixed hold duration (see
        # NotificationOverlay.set_horizon_active in notifications.py).
        # No camera-blocked alert, no tray icon changes, no posture/stress
        # tracking — those are exactly the "other notification types" that
        # should not appear during Horizon Mode.
        if self._horizon_active:
            looking_at_screen = bool(self._horizon_gaze and self._horizon_gaze.looking_at_screen)
            self.overlay.set_horizon_active(looking_at_screen)
            return

        # ── Calm / Unplug / recovery-wait: suppress everything else ─────────
        # Same principle as Horizon above — while Calm, Unplug, or the
        # post-Calm recovery decision owns the mode slot, none of the normal
        # RED/YELLOW/BLUE/GREEN notification path, camera-blocked alert, or
        # tray-icon-color tracking should run, and no meaningful-alert
        # accumulation should happen either.
        if self._active_mode in ('CALM', 'UNPLUG', 'RECOVERY_WAIT'):
            return

        live = self.detector.get_live()
        now  = time.time()

        calibrated = live.get('calibrated', False)
        face_ok    = live.get('face_detected', False) or live.get('pose_detected', False)

        # ── Camera-blocked detection ──────────────────────────────────────────
        # The detector has no concept of "camera physically blocked"; it just
        # stops seeing a face.  We handle it here after CAMERA_BLOCKED_TIMEOUT.
        if calibrated:
            if not face_ok:
                if self._no_detect_since is None:
                    self._no_detect_since = now
                elif (now - self._no_detect_since >= CAMERA_BLOCKED_TIMEOUT and
                      now - self._last_camera_alert_t >= CAMERA_ALERT_COOLDOWN):
                    self._last_camera_alert_t = now
                    self._camera_ok = False
                    # Camera alert goes through overlay directly (not via detector)
                    self.overlay.push('camera', [])
                    self._fire_camera_alert()
                    ts = now
                    QTimer.singleShot(0, lambda: self.dashboard.add_live_event(
                        ts, 'camera', 'Face not detected'))
            else:
                self._no_detect_since = None
                self._camera_ok = True

        # ── Tray icon colour — reflects current live state (no alert fired here) ──
        ps = live.get('posture_score', 0.0)
        ss = live.get('stress_score',  0.0)

        posture_active = calibrated and ps >= 0.45
        stress_active  = calibrated and ss >= 0.40

        # Track state transitions for icon only (no popup)
        if posture_active and not self._prev_posture_alerting:
            self._prev_posture_alerting = True
            if self._tray_state not in ('posture',):
                self._tray_state = 'posture'
                self._icon.setIcon(_make_canary_icon('posture'))
            # Phase 3: this rising edge is one "meaningful alert" — a
            # continuous posture problem only counts once, here, on the
            # transition into it; it will not count again until it clears
            # (posture_active goes False) and a new episode begins.
            self._on_meaningful_alert()
        elif not posture_active and self._prev_posture_alerting:
            self._prev_posture_alerting = False

        if stress_active and not self._prev_stress_alerting:
            self._prev_stress_alerting = True
            if self._tray_state not in ('posture',):
                self._tray_state = 'stress'
                self._icon.setIcon(_make_canary_icon('stress'))
            self._on_meaningful_alert()  # same edge-triggered accounting as posture above
        elif not stress_active and self._prev_stress_alerting:
            self._prev_stress_alerting = False
            if not posture_active and calibrated:
                self._tray_state = 'good'
                self._icon.setIcon(_make_canary_icon('good'))

        # ── Automatic Calm — sustained high stress with hysteresis ──────────
        # Independent of the stress_active/alert-popup threshold above:
        # CALM_STRESS_THRESHOLD is its own (higher) bar, and firing requires
        # holding above it continuously for CALM_STRESS_DURATION — a single
        # noisy frame can't trigger it. Re-arm requires a sustained drop
        # below CALM_RECOVERY_LEVEL, so one continuous high-stress episode
        # cannot repeatedly retrigger Calm.
        if (self._session_running and self._active_mode == 'IDLE' and
                calibrated and self._settings.get('calm_enabled', True)):
            if ss >= CALM_STRESS_THRESHOLD:
                if self._calm_high_stress_since is None:
                    self._calm_high_stress_since = now
                self._calm_recovered_since = None
                if (self._calm_rearmed and
                        now - self._calm_high_stress_since >= CALM_STRESS_DURATION):
                    self._calm_rearmed = False
                    self._calm_high_stress_since = None
                    self.trigger_calm_mode(for_unplug=False)
            else:
                self._calm_high_stress_since = None
                if ss < CALM_RECOVERY_LEVEL:
                    if self._calm_recovered_since is None:
                        self._calm_recovered_since = now
                    elif now - self._calm_recovered_since >= CALM_RECOVERY_DURATION:
                        self._calm_rearmed = True
                else:
                    self._calm_recovered_since = None

        # ── Tray tooltip ──────────────────────────────────────────────────────
        self._icon.setToolTip(
            f'Canary 🐦  |  Posture: {ps:.0%}  Stress: {ss:.0%}'
            f'  Blinks: {live.get("blink_rate", 0):.0f}/min')

    def _on_meaningful_alert(self):
        """
        Called on the rising edge of a posture or stress alert (see
        _poll_detector) — i.e. once per distinct sustained episode, not once
        per repeated notification/frame. Accumulates toward automatic
        Unplug and, at threshold, launches the Calm-first (or Unplug-direct,
        if Calm is disabled) chain — never both, and never while another
        mode already owns the screen.
        """
        if not self._settings.get('unplug_enabled', True):
            return  # disabled: don't even accumulate toward a no-op threshold

        self._meaningful_alert_count += 1
        self._session_meaningful_alert_total += 1

        if self._meaningful_alert_count < UNPLUG_ALERT_THRESHOLD:
            return
        if self._active_mode != 'IDLE':
            return  # re-checked at threshold time too — another mode may have started since
        if time.time() < self._unplug_cooldown_until:
            return  # still cooling down from a recent automatic Unplug

        if self._settings.get('calm_enabled', True):
            self.trigger_calm_mode(for_unplug=True)
        else:
            self.trigger_unplug_mode(automatic=True)

    def _on_alert(self, kind: str, score: float, signals: list = None):
        """
        Called by the detector background thread ONLY after a signal has been
        continuously active for SUSTAINED_SEC (currently 5 s) AND the per-kind
        cooldown has elapsed.  This is the authoritative alert source — it is
        based on real measured deviation from the user's personal baseline, NOT
        on a raw threshold crossing.

        We always show the overlay popup here.  _poll_detector handles the tray
        icon and live-dashboard event log separately (no double-overlay risk).
        """
        if self._active_mode != 'IDLE':
            # Horizon, Calm, Unplug, and the post-Calm recovery wait all
            # suppress every normal notification type (RED/YELLOW/BLUE/
            # GREEN) — this single check covers all of them, not just
            # Horizon, since they all now share the same _active_mode.
            return

        sigs = signals or []
        now  = time.time()

        # Always show the colour-coded overlay notification
        self.overlay.push(kind, sigs)

        # Update tray icon briefly
        icon_state = {'posture': 'posture', 'stress': 'stress',
                      'appreciation': 'good', 'water': 'water',
                      'camera': 'camera'}.get(kind, 'good')
        self._tray_state = icon_state
        self._icon.setIcon(_make_canary_icon(icon_state))
        QTimer.singleShot(8000, self._reset_icon)

        # Push to live event timeline (safe cross-thread via QTimer.singleShot)
        detail = ', '.join(sigs[:3]) if sigs else ''
        QTimer.singleShot(0, lambda: self.dashboard.add_live_event(
            now, kind, detail))

    def _fire_camera_alert(self):
        """Show tray notification for camera blocked."""
        self._tray_state = 'camera'
        self._icon.setIcon(_make_canary_icon('camera'))
        self._icon.showMessage(
            'Canary 🐦 — Camera Blocked',
            'Face not detected for 8 s. Posture & stress monitoring paused.\n'
            'Unblock your camera or adjust your position.',
            QSystemTrayIcon.Warning, 6000)
        QTimer.singleShot(10000, self._reset_icon)

    # ── Icon helpers ──────────────────────────────────────────────────────────

    def _reset_icon(self):
        if self.detector:
            if self._prev_posture_alerting:
                self._icon.setIcon(_make_canary_icon('posture'))
            elif self._prev_stress_alerting:
                self._icon.setIcon(_make_canary_icon('stress'))
            else:
                self._icon.setIcon(_make_canary_icon('good'))
        else:
            self._icon.setIcon(_make_canary_icon('idle'))

    # ── Quit ─────────────────────────────────────────────────────────────────

    def _quit(self):
        self._poll_timer.stop()
        if self.detector:
            try: self.detector.stop()
            except Exception: pass
            self.detector=None
        try: self.overlay.stop()
        except Exception: pass
        _save_settings(self.dashboard.get_settings())
        self._app.quit()