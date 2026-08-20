"""
core/detector.py  ─  Stress & Posture Awareness System
═══════════════════════════════════════════════════════════════════════════════
Detects webcam-visible stress and posture signals from the criteria:

  EYE signals
    blink_low      → reduced blink rate  (high cognitive load / screen stress)
    blink_high     → rapid blinking      (nervous tension / irritation)
    eye_narrow     → squinting / narrowed eyes (strain, frustration, fatigue)

  BROW / FOREHEAD signals
    brow_contract  → inner brow distance decrease (frustration, tension spike)
    brow_lower     → overall brow Y lowering  (anger, cognitive strain)

  MOUTH / JAW signals
    lip_press      → lip-gap decrease   (suppressed frustration, jaw tension)
    mouth_corner   → downturned corners (negative emotional state)

  OVERALL FACIAL signals
    facial_rigid   → reduced expressiveness (stress-related muscle tightening)

  HEAD / SCREEN INTERACTION signals  (Posture)
    forward_head   → face-area growth  (leaning toward screen / Tech Neck)
    head_droop     → nose-Y increase   (neck droop, "text neck")

  SHOULDER / UPPER BODY signals  (Posture)
    rounded_shoulder → shoulder Z increase (hunched / rounded shoulders)
    elevated_shoulder→ shoulder Y decrease (shoulders raised toward ears)

Alert categories
    "stress"  → any FACIAL or BROW or EYE or MOUTH signal sustained ≥ SUSTAINED_SEC
    "posture" → any HEAD or SHOULDER signal sustained ≥ SUSTAINED_SEC

MediaPipe compatibility
    mediapipe < 0.10.31  → legacy solutions API (FaceMesh + Pose)
    mediapipe ≥ 0.10.31  → new Tasks API  (downloads ~5 MB models on first run)
    No mediapipe         → OpenCV Haar-cascade fallback (reduced signals)
"""

import cv2
import numpy as np
import time
import threading
import urllib.request
import os
from collections import deque
from dataclasses import dataclass, field
from typing import List, Dict, Callable, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
#  FaceMesh landmark indices  (468-point canonical map)
# ─────────────────────────────────────────────────────────────────────────────
# Eyes — (outer, inner, top, bottom)
LEFT_EYE       = (263, 362, 386, 374)
RIGHT_EYE      = (33,  133, 160, 144)

# Brow inner corners (closest points to the nose)
BROW_L_INNER   = 336    # left inner brow
BROW_R_INNER   = 107    # right inner brow
# Brow mid-points (for overall height reference)
BROW_L_MID     = 285
BROW_R_MID     = 55

# Mouth
LIP_UPPER      = 13
LIP_LOWER      = 14
MOUTH_L_CORNER = 61
MOUTH_R_CORNER = 291

# Nose tip  (for head Y-position / droop detection)
NOSE_TIP       = 1

# Forehead reference (to measure face expressiveness range)
FOREHEAD_TOP   = 10

# Pose landmark indices (BlazePose 33-point)
POSE_NOSE              = 0
POSE_LEFT_SHOULDER     = 11
POSE_RIGHT_SHOULDER    = 12
POSE_LEFT_EAR          = 7
POSE_RIGHT_EAR         = 8

# Tasks-API model URLs
_FACE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)
_POSE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
)
_MODEL_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".stress_posture_models")


# ─────────────────────────────────────────────────────────────────────────────
#  Session data
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AlertEvent:
    timestamp: float
    kind:      str
    score:     float
    signals:   str = ""   # which signals triggered this alert


@dataclass
class SessionData:
    start_time: float = 0.0
    end_time:   float = 0.0

    good_posture_secs: float = 0.0
    bad_posture_secs:  float = 0.0
    no_detection_secs: float = 0.0

    posture_alerts:      int = 0
    stress_alerts:       int = 0
    appreciation_alerts: int = 0
    water_alerts:        int = 0

    stress_samples:  List[float] = field(default_factory=list)
    posture_samples: List[float] = field(default_factory=list)
    blink_samples:   List[float] = field(default_factory=list)

    events: List[AlertEvent] = field(default_factory=list)

    @property
    def duration_secs(self):
        end = self.end_time if self.end_time else time.time()
        return max(end - self.start_time, 1)

    @property
    def duration_min(self):
        return round(self.duration_secs / 60, 1)

    @property
    def good_posture_pct(self):
        det = self.good_posture_secs + self.bad_posture_secs
        return round(self.good_posture_secs / max(det, 1) * 100, 1)

    @property
    def avg_stress(self):
        return round(float(np.mean(self.stress_samples)) * 100, 1) \
               if self.stress_samples else 0.0

    @property
    def avg_posture(self):
        return round(float(np.mean(self.posture_samples)) * 100, 1) \
               if self.posture_samples else 0.0

    @property
    def avg_blink_rate(self):
        return round(float(np.mean(self.blink_samples)), 1) \
               if self.blink_samples else 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def _dist(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.linalg.norm(a - b))

def _pt(lm, idx: int) -> np.ndarray:
    return np.array([lm[idx].x, lm[idx].y])

def _eye_aspect_ratio(lm, indices: Tuple[int,int,int,int]) -> float:
    """EAR = vertical / horizontal distance."""
    pts = np.array([[lm[i].x, lm[i].y] for i in indices])
    h = _dist(pts[2], pts[3])
    w = _dist(pts[0], pts[1])
    return h / w if w > 1e-6 else 0.0

def _get_mp_version() -> Tuple[int, ...]:
    try:
        import mediapipe as mp
        return tuple(int(x) for x in mp.__version__.split(".")[:3])
    except Exception:
        return (0, 0, 0)

def _download_model(url: str, filename: str) -> Optional[str]:
    os.makedirs(_MODEL_CACHE_DIR, exist_ok=True)
    dest = os.path.join(_MODEL_CACHE_DIR, filename)
    if os.path.exists(dest):
        return dest
    try:
        urllib.request.urlretrieve(url, dest)
        return dest
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  _MPBackend — version-agnostic mediapipe wrapper
# ─────────────────────────────────────────────────────────────────────────────

class _MPBackend:
    """
    Picks the correct mediapipe API at runtime and exposes a single
    process(rgb) → Dict interface regardless of version.
    """
    MODE_LEGACY = "legacy"    # mp.solutions.*  (< 0.10.31)
    MODE_TASKS  = "tasks"     # mp.tasks.*      (>= 0.10.31)
    MODE_OPENCV = "opencv"    # Haar cascades   (fallback)

    def __init__(self):
        self._mode        = self.MODE_OPENCV
        self._face_proc   = None
        self._pose_proc   = None
        self._face_land   = None
        self._pose_land   = None
        self._ear_prev    = 0.0
        self._blink_times: deque = deque(maxlen=400)
        self._cv_face_cas = None
        self._cv_eye_cas  = None
        self._init()

    def _init(self):
        ver = _get_mp_version()
        if ver == (0, 0, 0):
            self._init_opencv(); return
        if ver < (0, 10, 31):
            if self._try_legacy(): return
        if self._try_tasks(): return
        self._init_opencv()

    def _try_legacy(self) -> bool:
        try:
            import mediapipe as mp
            self._face_proc = mp.solutions.face_mesh.FaceMesh(
                max_num_faces=1, refine_landmarks=False,
                min_detection_confidence=0.5, min_tracking_confidence=0.5)
            self._pose_proc = mp.solutions.pose.Pose(
                static_image_mode=False, model_complexity=1,
                min_detection_confidence=0.5, min_tracking_confidence=0.5)
            self._mode = self.MODE_LEGACY
            return True
        except Exception:
            return False

    def _try_tasks(self) -> bool:
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_tasks
            from mediapipe.tasks.python import vision as mp_vision
            face_path = _download_model(_FACE_MODEL_URL, "face_landmarker.task")
            pose_path = _download_model(_POSE_MODEL_URL, "pose_landmarker_lite.task")
            if not face_path or not pose_path:
                return False
            self._face_land = mp_vision.FaceLandmarker.create_from_options(
                mp_vision.FaceLandmarkerOptions(
                    base_options=mp_tasks.BaseOptions(model_asset_path=face_path),
                    num_faces=1,
                    min_face_detection_confidence=0.5,
                    min_face_presence_confidence=0.5,
                    min_tracking_confidence=0.5,
                    output_face_blendshapes=False,
                    output_facial_transformation_matrixes=False,
                ))
            self._pose_land = mp_vision.PoseLandmarker.create_from_options(
                mp_vision.PoseLandmarkerOptions(
                    base_options=mp_tasks.BaseOptions(model_asset_path=pose_path),
                    num_poses=1,
                    min_pose_detection_confidence=0.5,
                    min_pose_presence_confidence=0.5,
                    min_tracking_confidence=0.5,
                ))
            self._mode = self.MODE_TASKS
            return True
        except Exception:
            return False

    def _init_opencv(self):
        self._mode = self.MODE_OPENCV
        try:
            self._cv_face_cas = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            self._cv_eye_cas = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_eye.xml")
        except Exception:
            pass

    @property
    def mode(self) -> str:
        return self._mode

    def process(self, rgb: np.ndarray) -> Dict:
        now = time.time()
        if self._mode == self.MODE_LEGACY:  return self._proc_legacy(rgb, now)
        if self._mode == self.MODE_TASKS:   return self._proc_tasks(rgb, now)
        return self._proc_opencv(rgb, now)

    def close(self):
        for attr in ('_face_proc', '_pose_proc', '_face_land', '_pose_land'):
            obj = getattr(self, attr, None)
            if obj:
                try: obj.close()
                except Exception: pass
                setattr(self, attr, None)

    # ── Legacy solutions API ──────────────────────────────────────────────────
    def _proc_legacy(self, rgb: np.ndarray, now: float) -> Dict:
        m = {'face_detected': False, 'pose_detected': False}
        r = self._face_proc.process(rgb)
        if r.multi_face_landmarks:
            self._fill_face(m, r.multi_face_landmarks[0].landmark, now)
        m['blink_rate'] = self._blink_count(now)
        r2 = self._pose_proc.process(rgb)
        if r2.pose_landmarks:
            self._fill_pose(m, r2.pose_landmarks.landmark)
        return m

    # ── Tasks API ─────────────────────────────────────────────────────────────
    def _proc_tasks(self, rgb: np.ndarray, now: float) -> Dict:
        import mediapipe as mp
        m   = {'face_detected': False, 'pose_detected': False}
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        r   = self._face_land.detect(img)
        if r.face_landmarks:
            self._fill_face(m, r.face_landmarks[0], now)
        m['blink_rate'] = self._blink_count(now)
        r2 = self._pose_land.detect(img)
        if r2.pose_landmarks:
            self._fill_pose(m, r2.pose_landmarks[0])
        return m

    # ── OpenCV Haar fallback ──────────────────────────────────────────────────
    def _proc_opencv(self, rgb: np.ndarray, now: float) -> Dict:
        m = {'face_detected': False, 'pose_detected': False, 'blink_rate': 0}
        if self._cv_face_cas is None:
            return m
        gray  = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        h, w  = gray.shape
        faces = self._cv_face_cas.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
        if len(faces) == 0:
            m['blink_rate'] = self._blink_count(now)
            return m
        fx, fy, fw, fh = max(faces, key=lambda r: r[2] * r[3])
        m['face_detected'] = True
        m['face_size']     = (fw / w) * (fh / h)
        m['head_y']        = (fy + fh * 0.35) / h   # nose ~35% down from face top
        eye_roi = gray[fy: fy + fh // 2, fx: fx + fw]
        eyes = []
        if self._cv_eye_cas is not None:
            eyes = self._cv_eye_cas.detectMultiScale(
                eye_roi, scaleFactor=1.1, minNeighbors=3, minSize=(15, 15))
        if len(eyes) >= 2:
            s_eyes = sorted(eyes, key=lambda e: e[0])[:2]
            ears   = [eh / max(ew, 1) for (_, _, ew, eh) in s_eyes]
            avg_e  = float(np.mean(ears))
            m['eye_ear'] = avg_e
            if self._ear_prev > 0 and avg_e < 0.32 and self._ear_prev >= 0.32:
                self._blink_times.append(now)
            self._ear_prev = avg_e
            cx = [ex + ew // 2 for (ex, _, ew, _) in s_eyes]
            m['brow_inner_dist'] = abs(cx[0] - cx[1]) / w
        elif len(eyes) == 1:
            _, _, ew, eh = eyes[0]
            m['eye_ear'] = eh / max(ew, 1)
        else:
            if self._ear_prev > 0.32:
                self._blink_times.append(now)
            self._ear_prev = 0.0
            m['eye_ear']   = 0.0
        m['blink_rate'] = self._blink_count(now)
        lip_roi = gray[fy + int(fh * 0.62): fy + fh, fx: fx + fw]
        if lip_roi.size > 0:
            _, th = cv2.threshold(lip_roi, 80, 255, cv2.THRESH_BINARY_INV)
            m['lip_gap'] = float(np.std(np.sum(th, axis=1) / max(fw, 1))) / 255.0
        return m

    # ── Shared face landmark fill ─────────────────────────────────────────────
    def _fill_face(self, m: Dict, lm, now: float):
        """Extract ALL face metrics from a 468-point landmark list."""
        m['face_detected'] = True

        # ── Eye aspect ratio (both eyes, averaged) ────────────────────────────
        el  = _eye_aspect_ratio(lm, LEFT_EYE)
        er  = _eye_aspect_ratio(lm, RIGHT_EYE)
        ear = (el + er) / 2.0
        m['eye_ear'] = ear

        # Blink: EAR crosses below threshold
        if self._ear_prev > 0 and ear < 0.20 and self._ear_prev >= 0.20:
            self._blink_times.append(now)
        self._ear_prev = ear

        # ── Brow inner distance ───────────────────────────────────────────────
        # Distance between the two inner brow corners
        bl_in = _pt(lm, BROW_L_INNER)
        br_in = _pt(lm, BROW_R_INNER)
        m['brow_inner_dist'] = _dist(bl_in, br_in)

        # Brow mid-point average Y (for brow-lowering detection)
        brow_y = (lm[BROW_L_MID].y + lm[BROW_R_MID].y) / 2.0
        m['brow_y'] = brow_y

        # ── Lip gap & mouth corners ───────────────────────────────────────────
        lip_u = _pt(lm, LIP_UPPER)
        lip_l = _pt(lm, LIP_LOWER)
        m['lip_gap'] = _dist(lip_u, lip_l)

        # Mouth corner height relative to lip centre Y
        # Positive = corners above centre (smile), negative = corners below (frown)
        lip_centre_y = (lm[LIP_UPPER].y + lm[LIP_LOWER].y) / 2.0
        corner_y = (lm[MOUTH_L_CORNER].y + lm[MOUTH_R_CORNER].y) / 2.0
        # corner_delta > 0 means corners are BELOW lip centre = downturned
        m['mouth_corner_delta'] = corner_y - lip_centre_y

        # ── Face bounding area (for forward-lean / face-size change) ──────────
        xs = [p.x for p in lm]
        ys = [p.y for p in lm]
        m['face_size'] = (max(xs) - min(xs)) * (max(ys) - min(ys))

        # ── Head position ─────────────────────────────────────────────────────
        # Nose tip Y  (increases as head droops forward/down)
        m['head_y'] = lm[NOSE_TIP].y

        # Face vertical range: distance from forehead top to chin area
        # Shrinks when head droops in profile view but useful as expressiveness proxy
        m['face_height'] = lm[FOREHEAD_TOP].y - lm[NOSE_TIP].y   # negative = forehead above nose

    # ── Pose landmark fill ────────────────────────────────────────────────────
    def _fill_pose(self, m: Dict, plm):
        """Extract shoulder & head posture signals from pose landmarks."""
        m['pose_detected'] = True

        ls   = plm[POSE_LEFT_SHOULDER]
        rs   = plm[POSE_RIGHT_SHOULDER]
        nose = plm[POSE_NOSE]
        le   = plm[POSE_LEFT_EAR]
        re   = plm[POSE_RIGHT_EAR]

        # Shoulder mid-point
        smx = (ls.x + rs.x) / 2
        smy = (ls.y + rs.y) / 2
        smz = (ls.z + rs.z) / 2

        # Forward head: nose Z vs shoulder Z
        # In normalised coords: smaller z = closer to camera
        # Forward head posture: nose comes closer (more negative z) relative to shoulders
        m['head_forward_z']   = nose.z - smz        # negative → head in front of shoulders
        m['shoulder_forward'] = -smz                 # increases when shoulders round forward

        # Shoulder elevation: average shoulder Y (decreases = shoulders raised toward ears)
        m['shoulder_y'] = smy

        # Ear-to-shoulder offset: how far ears are in front of shoulders in Z
        # More negative = ears/head further forward than shoulders (Tech Neck)
        ear_z  = (le.z + re.z) / 2
        m['ear_shoulder_z'] = ear_z - smz

    def _blink_count(self, now: float) -> int:
        """Blinks in the last 30 seconds."""
        return sum(1 for t in self._blink_times if now - t < 30)


# ─────────────────────────────────────────────────────────────────────────────
#  StressPostureDetector
# ─────────────────────────────────────────────────────────────────────────────

class StressPostureDetector:
    """
    Calibrates a personal baseline in ~30 s, then continuously checks
    deviation from that baseline across all webcam-detectable stress
    and posture signals.

    Signal groups
    ─────────────
    STRESS signals  (facial / eye / brow / mouth)
      blink_low      reduced blink rate (< 45 % of baseline)
      blink_high     rapid blinking     (> 220 % of baseline)
      eye_narrow     EAR drops ≥ 8 %   (squinting / eye strain)
      brow_contract  inner-brow distance drops ≥ 7 % (furrowed brows)
      brow_lower     brow Y increases ≥ 5 % (brows pulled down)
      lip_press      lip-gap drops ≥ 10 % (pressed lips / jaw tension)
      mouth_down     mouth-corner delta increases ≥ 15 % (downturned mouth)

    POSTURE signals  (head / shoulder / body)
      forward_head   face-area grows ≥ 10 % (leaning toward screen)
      head_droop     nose-Y increases ≥ 6 %  (head drooping down)
      rounded_shld   shoulder-forward increases ≥ 7 % (hunching)
      elevated_shld  shoulder-Y drops ≥ 5 %  (shoulders raised toward ears)
      tech_neck      ear-shoulder Z delta ≥ 8 % (ear ahead of shoulder)

    Alert rules
    ───────────
      "stress"  → any 1+ STRESS signal sustained ≥ SUSTAINED_SEC
      "posture" → any 1+ POSTURE signal sustained ≥ SUSTAINED_SEC
    """

    # ── Timing ────────────────────────────────────────────────────────────────
    CALIB_DURATION_SEC      = 30     # hard cap on calibration window
    MIN_CALIB_SAMPLES       = 20     # complete calibration early if ≥ 20 clean frames
    CALIB_SAMPLE_INTERVAL   = 0.35   # sample rate during calibration
    SUSTAINED_SEC           = 5.0    # signal must hold this long before firing alert
    HISTORY_SAMPLE_INTERVAL = 0.35   # live history sample rate

    # ── Alert cooldowns ───────────────────────────────────────────────────────
    COOLDOWNS = {
        'posture':      30,
        'stress':       45,
        'appreciation': 300,
        'water':        600,
    }

    # ── Detection thresholds (fractional deviation from personal baseline) ────
    # Eye
    BLINK_NORMAL_LOW   = 4       # floor for blink baseline (blinks / 30 s)
    BLINK_LOW_RATIO    = 0.45    # under-blinking → screen stress
    BLINK_HIGH_RATIO   = 2.2     # over-blinking  → nervous tension
    EYE_NARROW_PCT     = 0.12    # 8% EAR drop → squinting

    # Brow
    BROW_CONTRACT_PCT  = 0.12    # 7% inner-brow distance decrease → furrow
    BROW_LOWER_PCT     = 0.09    # 5% brow-Y increase             → brows pulled down

    # Mouth
    LIP_PRESS_PCT      = 0.14    # 10% lip-gap decrease → pressed lips
    MOUTH_DOWN_PCT     = 0.20    # 15% corner-delta increase → downturned mouth

    # Head
    FACE_FORWARD_PCT   = 0.14    # 10% face-area growth → leaning toward screen
    HEAD_DROOP_PCT     = 0.09    # 6% nose-Y increase   → head drooping

    # Shoulders
    SHOULDER_FWD_PCT   = 0.10    # 7% shoulder-forward increase → rounded shoulders
    SHOULDER_ELEV_PCT  = 0.08    # 5% shoulder-Y decrease       → elevated shoulders
    TECH_NECK_PCT      = 0.11    # 8% ear-shoulder Z increase    → tech neck

    GOOD_STREAK_SECS   = 300     # 5-min good-posture streak → appreciation

    STRESS_SIGNALS  = (
        'blink_low', 'blink_high', 'eye_narrow',
        'brow_contract', 'brow_lower',
        'lip_press', 'mouth_down',
    )
    POSTURE_SIGNALS = (
        'forward_head', 'head_droop',
        'rounded_shld', 'elevated_shld', 'tech_neck',
    )
    ALL_SIGNALS = STRESS_SIGNALS + POSTURE_SIGNALS

    def __init__(self, on_alert: Optional[Callable[[str, float], None]] = None):
        self.on_alert = on_alert
        self._backend = _MPBackend()
        self.session  = SessionData()

        # Optional external tap into the raw camera frames, set only by
        # Horizon Mode (see ui/horizon/horizon_gaze.py) while its own
        # gaze/head-pose check needs frames at real camera framerate —
        # much faster than the UI's 500ms poll cycle allows. None of
        # the main detection logic reads or depends on this; it's
        # purely an optional side-channel for a separate component.
        # Wrapped in try/except at the call site so a bug here can
        # never take down the main detector thread.
        self.frame_hook: Optional[Callable[[np.ndarray], None]] = None

        # Personal baseline (medians captured during calibration)
        self._baseline: Dict[str, Optional[float]] = {
            'blink_rate': None,
            'eye_ear':    None,
            'brow_inner_dist': None,
            'brow_y':    None,
            'lip_gap':   None,
            'mouth_corner_delta': None,
            'face_size': None,
            'head_y':    None,
            'shoulder_forward':  None,
            'shoulder_y':        None,
            'ear_shoulder_z':    None,
        }
        self._calib:       Dict[str, list] = {k: [] for k in self._baseline}
        self._calib_start: Optional[float] = None
        self._calib_done:  bool            = False

        # Rolling metric histories  (covers ~4 s at 2.5 Hz → 10 samples)
        N = 20
        self._hist: Dict[str, deque] = {
            'eye_ear':           deque(maxlen=N),
            'brow_inner_dist':   deque(maxlen=N),
            'brow_y':            deque(maxlen=N),
            'lip_gap':           deque(maxlen=N),
            'mouth_corner_delta':deque(maxlen=N),
            'face_size':         deque(maxlen=N),
            'head_y':            deque(maxlen=N),
            'shoulder_forward':  deque(maxlen=N),
            'shoulder_y':        deque(maxlen=N),
            'ear_shoulder_z':    deque(maxlen=N),
        }

        self._sustained_since: Dict[str, Optional[float]] = {
            s: None for s in self.ALL_SIGNALS
        }
        self._last_alert = {k: 0.0 for k in self.COOLDOWNS}
        self._good_streak_start: Optional[float] = None

        self._live:     Dict = {}
        self._live_lock = threading.Lock()
        self._running   = False
        self._thread:   Optional[threading.Thread] = None
        self._frame_n   = 0
        self._last_calib_t = 0.0
        self._last_hist_t  = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        self.session          = SessionData(start_time=time.time())
        self._calib_start     = time.time()
        self._calib_done      = False
        for k in self._calib:     self._calib[k].clear()
        for k in self._baseline:  self._baseline[k] = None
        for k in self._sustained_since: self._sustained_since[k] = None
        for h in self._hist.values(): h.clear()
        self._last_alert      = {k: 0.0 for k in self.COOLDOWNS}
        self._good_streak_start = None
        self._last_calib_t    = 0.0
        self._last_hist_t     = 0.0
        self._running         = True
        self._thread          = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> SessionData:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self.session.end_time = time.time()
        self._backend.close()
        return self.session

    def get_live(self) -> Dict:
        with self._live_lock:
            return dict(self._live)

    def is_calibrated(self) -> bool:
        return self._calib_done

    def calib_progress(self) -> float:
        if self._calib_start is None: return 0.0
        if self._calib_done:          return 100.0
        time_pct   = (time.time() - self._calib_start) / self.CALIB_DURATION_SEC * 100
        counts     = [len(v) for v in self._calib.values() if v]
        sample_pct = (min(counts) / self.MIN_CALIB_SAMPLES * 100) if counts else 0
        return min(max(time_pct, sample_pct), 99.9)

    @property
    def detection_mode(self) -> str:
        return self._backend.mode

    # ── Camera loop ───────────────────────────────────────────────────────────

    def _loop(self):
        cap = None
        for idx in range(4):
            c = cv2.VideoCapture(idx)
            if c.isOpened():
                cap = c; break
        if cap is None:
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 15)
        while self._running:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.05); continue
            self._frame_n += 1
            if self._frame_n % 2 == 0:          # ~7 fps effective
                flipped = cv2.flip(frame, 1)
                self._process(flipped)
                if self.frame_hook is not None:
                    try:
                        self.frame_hook(flipped)
                    except Exception:
                        # A problem in Horizon Mode's gaze monitor must
                        # never be able to take down the main detector
                        # loop — this component is intentionally
                        # decoupled from it.
                        pass
            time.sleep(0.033)
        cap.release()

    # ── Frame processing ──────────────────────────────────────────────────────

    def _process(self, frame: np.ndarray):
        now = time.time()
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        m   = self._backend.process(rgb)

        # ── Calibration ───────────────────────────────────────────────────────
        if not self._calib_done and self._calib_start is not None:
            if now - self._last_calib_t >= self.CALIB_SAMPLE_INTERVAL:
                self._last_calib_t = now
                if m.get('face_detected'):
                    for key in self._calib:
                        if m.get(key) is not None:
                            self._calib[key].append(m[key])
                    # blink_rate is face-independent but accumulates naturally
                    self._calib['blink_rate'].append(float(m.get('blink_rate', 0)))

                # Session accounting during calibration
                self.session.stress_samples.append(0.0)
                self.session.posture_samples.append(0.0)
                self.session.blink_samples.append(float(m.get('blink_rate', 0)))
                if m.get('face_detected') or m.get('pose_detected'):
                    self.session.good_posture_secs += self.CALIB_SAMPLE_INTERVAL
                    if self._good_streak_start is None:
                        self._good_streak_start = now
                else:
                    self.session.no_detection_secs += self.CALIB_SAMPLE_INTERVAL
                    self._good_streak_start = None

            # Check if calibration is complete
            elapsed        = now - self._calib_start
            face_counts    = [len(v) for v in self._calib.values() if v]
            enough_samples = bool(face_counts) and min(face_counts) >= self.MIN_CALIB_SAMPLES
            time_expired   = elapsed >= self.CALIB_DURATION_SEC

            if enough_samples or time_expired:
                self._finalise_calibration()

        # ── Compute signal states ─────────────────────────────────────────────
        states        = self._compute_states(m)
        stress_score  = self._stress_score(states)
        posture_score = self._posture_score(states, m)

        # ── Update rolling histories ──────────────────────────────────────────
        if now - self._last_hist_t >= self.HISTORY_SAMPLE_INTERVAL:
            self._last_hist_t = now
            for key, hist in self._hist.items():
                if m.get(key) is not None:
                    hist.append(m[key])

            if self._calib_done:
                self.session.stress_samples.append(stress_score)
                self.session.posture_samples.append(posture_score)
                self.session.blink_samples.append(float(m.get('blink_rate', 0)))
                if m.get('face_detected') or m.get('pose_detected'):
                    if posture_score < 0.4:
                        self.session.good_posture_secs += self.HISTORY_SAMPLE_INTERVAL
                        if self._good_streak_start is None:
                            self._good_streak_start = now
                    else:
                        self.session.bad_posture_secs += self.HISTORY_SAMPLE_INTERVAL
                        self._good_streak_start = None
                else:
                    self.session.no_detection_secs += self.HISTORY_SAMPLE_INTERVAL
                    self._good_streak_start = None

        # ── Publish live data ─────────────────────────────────────────────────
        m.update(
            stress_score   = round(stress_score,  2),
            posture_score  = round(posture_score, 2),
            calibrated     = self._calib_done,
            calib_pct      = round(self.calib_progress(), 1),
            active_signals = [s for s in self.ALL_SIGNALS if states.get(s)],
            detection_mode = self._backend.mode,
        )
        with self._live_lock:
            self._live = dict(m)

        if self._calib_done:
            self._check_alerts(states, posture_score, stress_score, now)

    def _finalise_calibration(self):
        """Lock in median baselines, filling missing ones with sensible defaults."""
        DEFAULTS = {
            'blink_rate':        5.0,
            'eye_ear':           0.28,
            'brow_inner_dist':   0.10,
            'brow_y':            0.38,
            'lip_gap':           0.025,
            'mouth_corner_delta':0.02,
            'face_size':         0.07,
            'head_y':            0.42,
            'shoulder_forward':  0.05,
            'shoulder_y':        0.50,
            'ear_shoulder_z':   -0.05,
        }
        for k in self._baseline:
            v = self._calib.get(k, [])
            self._baseline[k] = float(np.median(v)) if v else DEFAULTS.get(k)
        self._calib_done = True

    # ── Signal computation ────────────────────────────────────────────────────

    def _compute_states(self, m: Dict) -> Dict[str, bool]:
        """Return dict of signal_name → True if currently deviating from baseline."""
        states = {s: False for s in self.ALL_SIGNALS}
        if not self._calib_done:
            return states

        MIN_H = 8   # need ≥ 4 history samples before judging (~1.4 s)

        def ravg(key: str, n: int = 12) -> Optional[float]:
            h = self._hist.get(key)
            if h is None or len(h) < MIN_H:
                return None
            data = list(h)[-n:]
            return float(np.mean(data))

        def base(key: str) -> Optional[float]:
            return self._baseline.get(key)

        def pct_drop(key: str, threshold: float) -> bool:
            """True if recent avg has DROPPED by at least threshold from baseline."""
            cur = ravg(key); b = base(key)
            if cur is None or b is None or b <= 0: return False
            return (b - cur) / b >= threshold

        def pct_rise(key: str, threshold: float) -> bool:
            """True if recent avg has RISEN by at least threshold from baseline."""
            cur = ravg(key); b = base(key)
            if cur is None or b is None: return False
            ref = abs(b) if b != 0 else 1e-6
            return (cur - b) / ref >= threshold

        # ── Eye signals ───────────────────────────────────────────────────────
        br   = float(m.get('blink_rate', 0))
        b_br = base('blink_rate') or self.BLINK_NORMAL_LOW
        if br < max(b_br * self.BLINK_LOW_RATIO, 1.5):
            states['blink_low']  = True
        if br > b_br * self.BLINK_HIGH_RATIO:
            states['blink_high'] = True
        if pct_drop('eye_ear', self.EYE_NARROW_PCT):
            states['eye_narrow'] = True

        # ── Brow signals ──────────────────────────────────────────────────────
        if pct_drop('brow_inner_dist', self.BROW_CONTRACT_PCT):
            states['brow_contract'] = True
        # brow_y increasing = brows moving DOWN (Y axis goes down in image coords)
        if pct_rise('brow_y', self.BROW_LOWER_PCT):
            states['brow_lower'] = True

        # ── Mouth / jaw signals ───────────────────────────────────────────────
        if pct_drop('lip_gap', self.LIP_PRESS_PCT):
            states['lip_press'] = True
        # mouth_corner_delta rising = corners pulling DOWN (frown)
        if pct_rise('mouth_corner_delta', self.MOUTH_DOWN_PCT):
            states['mouth_down'] = True

        # ── Head posture signals ──────────────────────────────────────────────
        # face_size grows → leaning toward screen
        if pct_rise('face_size', self.FACE_FORWARD_PCT):
            states['forward_head'] = True
        # head_y rises → nose moving down in frame (head drooping)
        if pct_rise('head_y', self.HEAD_DROOP_PCT):
            states['head_droop'] = True

        # ── Shoulder / body signals ───────────────────────────────────────────
        # shoulder_forward rises → shoulders rounding forward
        if pct_rise('shoulder_forward', self.SHOULDER_FWD_PCT):
            states['rounded_shld'] = True
        # shoulder_y drops → shoulders elevated toward ears
        if pct_drop('shoulder_y', self.SHOULDER_ELEV_PCT):
            states['elevated_shld'] = True
        # ear_shoulder_z rises → ears further in front of shoulders (tech neck)
        if pct_rise('ear_shoulder_z', self.TECH_NECK_PCT):
            states['tech_neck'] = True

        return states

    # ── Sustained-signal tracking ─────────────────────────────────────────────

    def _update_sustained(self, states: Dict[str, bool], now: float) -> Dict[str, bool]:
        """Track how long each signal has been continuously active."""
        sustained = {}
        for sig, active in states.items():
            if active:
                if self._sustained_since[sig] is None:
                    self._sustained_since[sig] = now
                if now - self._sustained_since[sig] >= self.SUSTAINED_SEC:
                    sustained[sig] = True
            else:
                self._sustained_since[sig] = None   # reset clock when signal clears
        return sustained

    # ── Score aggregation ─────────────────────────────────────────────────────

    def _stress_score(self, states: Dict[str, bool]) -> float:
        n = sum(1 for s in self.STRESS_SIGNALS if states.get(s))
        return min(0.2 + n * 0.2, 1.0) if n >= 1 else 0.0

    def _posture_score(self, states: Dict[str, bool], m: Dict) -> float:
        n = sum(1 for s in self.POSTURE_SIGNALS if states.get(s))
        if n >= 1:
            return min(0.4 + n * 0.2, 1.0)
        if not m.get('face_detected') and not m.get('pose_detected'):
            return 0.3
        return 0.0

    # ── Alert firing ─────────────────────────────────────────────────────────

    def _check_alerts(self, states: Dict[str, bool], posture: float,
                      stress: float, now: float):
        sustained = self._update_sustained(states, now)

        def fire(kind: str, score: float, which: List[str]):
            self._last_alert[kind] = now
            sig_str = ', '.join(which)
            self.session.events.append(AlertEvent(now, kind, score, sig_str))
            attr = f'{kind}_alerts'
            if hasattr(self.session, attr):
                setattr(self.session, attr, getattr(self.session, attr) + 1)
            if self.on_alert:
                self.on_alert(kind, score, which)

        # ── Posture alert ─────────────────────────────────────────────────────
        posture_sus = [s for s in self.POSTURE_SIGNALS if sustained.get(s)]
        if posture_sus:
            if now - self._last_alert['posture'] >= self.COOLDOWNS['posture']:
                fire('posture', posture, posture_sus)

        # ── Stress alert ──────────────────────────────────────────────────────
        stress_sus = [s for s in self.STRESS_SIGNALS if sustained.get(s)]
        if stress_sus:
            if now - self._last_alert['stress'] >= self.COOLDOWNS['stress']:
                fire('stress', stress, stress_sus)

        # ── Appreciation (10-min good-posture streak) ─────────────────────────
        if (self._good_streak_start is not None
                and now - self._good_streak_start >= self.GOOD_STREAK_SECS):
            if now - self._last_alert['appreciation'] >= self.COOLDOWNS['appreciation']:
                fire('appreciation', 1.0, ['good_posture_streak'])
                self._good_streak_start = now

        # ── Water break (every 30 min) ────────────────────────────────────────
        elapsed   = now - self.session.start_time
        water_due = int(elapsed // self.COOLDOWNS['water'])
        if water_due > self.session.water_alerts:
            self.session.water_alerts = water_due
            self._last_alert['water'] = now
            self.session.events.append(AlertEvent(now, 'water', 0.0, 'timer'))
            if self.on_alert:
                self.on_alert('water', 0.0, [])
