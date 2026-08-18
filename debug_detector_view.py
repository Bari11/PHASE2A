#!/usr/bin/env python3
"""
debug_detector_view.py  —  Real-Time Debug View
════════════════════════════════════════════════
Full-screen camera feed with rich skeleton overlay exactly matching
the detector's internal signal processing, plus live metric panels.

Layout (mirrors the screenshot reference)
──────────────────────────────────────────
  ┌─────────────────────────────────────────────────┐
  │ Posture: Bad  Stress: Low  EAR: 0.24  Blinks: 10│  ← HUD bar
  ├─────────────────────────────────────────────────┤
  │                              ┌─────────────────┐│
  │                              │● Sit Straight   ││  ← alert badge (top-right)
  │   FULL WEBCAM + MESH         │  Uneven shoulder││
  │   white face mesh            └─────────────────┘│
  │   coloured key features                         │
  │   green pose skeleton                           │
  │                                                 │
  │ Spine angle:  157.3 deg   Head drop: -0.384     │
  │ Neck forward: 0.001                             │
  ├─────────────────────────────────────────────────┤
  │ STRESS MONITOR   NEUTRAL :-|   Stress: Low      │
  │ ANGRY ████░░░░   1.4                            │
  │ Signals detected: Angry: tight/pressed lips(35%)│
  │ Raw posture: Bad  Smoothed: Bad                 │
  └─────────────────────────────────────────────────┘

Usage
─────
  python debug_detector_view.py           # cam 0
  python debug_detector_view.py --cam 1
  Q/ESC = quit    C = skip calibration    F = toggle fullscreen
"""

import sys, os, time, argparse, collections, math
from typing import Dict, Deque, Optional, List, Tuple
import cv2
import numpy as np

# ── path ──────────────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core.detector import (
    StressPostureDetector, SessionData,
    _FACE_MODEL_URL, _POSE_MODEL_URL, _download_model,
    LEFT_EYE, RIGHT_EYE,
    BROW_L_INNER, BROW_R_INNER, BROW_L_MID, BROW_R_MID,
    LIP_UPPER, LIP_LOWER, MOUTH_L_CORNER, MOUTH_R_CORNER,
    NOSE_TIP, FOREHEAD_TOP,
    POSE_NOSE, POSE_LEFT_SHOULDER, POSE_RIGHT_SHOULDER,
    POSE_LEFT_EAR, POSE_RIGHT_EAR,
)

# ── BGR colour palette ────────────────────────────────────────────────────────
C_BG      = (12,  14,  20)
C_HUD     = (16,  20,  32)
C_PANEL   = (18,  22,  38)
C_BORDER  = (42,  52,  72)
C_WHITE   = (230, 232, 238)
C_LGRAY   = (160, 165, 178)
C_GRAY    = (100, 108, 124)
C_GREEN   = ( 50, 210,  70)
C_YELLOW  = ( 30, 195, 230)
C_RED     = ( 45,  55, 215)
C_ORANGE  = ( 40, 140, 255)
C_CYAN    = (210, 215,  45)
C_PURPLE  = (200,  80, 210)
C_BLUE    = (220, 145,  45)
C_LIME    = ( 30, 230, 120)

# ── extra face-mesh connection groups for rich overlay ────────────────────────
# Full face oval (contour)
# ── Verified canonical MediaPipe 468-point chains ─────────────────────────────
# Derived from mediapipe/python/solutions/face_mesh_connections.py
# Each list is an ordered sequence for smooth polylines.

# Face silhouette — closed loop (36 pts)
FACE_OVAL = [10,109,67,103,54,21,162,127,234,93,132,58,172,136,150,149,
             176,148,152,377,400,378,379,365,397,288,361,323,454,356,389,
             251,284,332,297,338,10]

# Eyebrows — open arcs, inner→outer (10 pts each)
# BROW_L = subject's LEFT brow (appears on screen RIGHT in mirrored feed)
BROW_L_FULL = [276,283,282,295,285,300,293,334,296,336]
# BROW_R = subject's RIGHT brow (appears on screen LEFT in mirrored feed)
BROW_R_FULL = [46,53,52,65,55,70,63,105,66,107]

# Eyes — closed loops derived from all 16 connection pairs
LEFT_EYE_FULL  = [249,263,466,388,387,386,385,384,398,362,382,381,380,374,373,390,249]
RIGHT_EYE_FULL = [7,33,246,161,160,159,158,157,173,133,155,154,153,145,144,163,7]

# Lips — outer closed loop
LIP_OUTER = [0,37,39,40,185,61,146,91,181,84,17,314,405,321,375,291,409,270,269,267,0]
# Lips — inner closed loop
LIP_INNER = [78,191,80,81,82,13,312,311,310,415,308,324,318,402,317,14,87,178,88,95,78]

# Nose — bridge line top→tip, plus nostril wings
NOSE_BRIDGE = [168,6,197,195,5,4,1]
NOSE_L_WING = [129,102,49,48,115,220,45,4]
NOSE_R_WING = [358,331,279,278,344,440,275,4]

# Cheek landmarks (for face oval shading)
POSE_LEFT_ELBOW  = 13
POSE_RIGHT_ELBOW = 14
POSE_LEFT_WRIST  = 15
POSE_RIGHT_WRIST = 16
POSE_LEFT_HIP    = 23
POSE_RIGHT_HIP   = 24
POSE_LEFT_KNEE   = 25
POSE_RIGHT_KNEE  = 26

# Full pose connection pairs (BlazePose 33)
POSE_CONNECTIONS = [
    (POSE_LEFT_EAR, POSE_LEFT_SHOULDER),
    (POSE_RIGHT_EAR, POSE_RIGHT_SHOULDER),
    (POSE_LEFT_SHOULDER, POSE_RIGHT_SHOULDER),
    (POSE_LEFT_SHOULDER, POSE_LEFT_ELBOW),
    (POSE_RIGHT_SHOULDER, POSE_RIGHT_ELBOW),
    (POSE_LEFT_ELBOW, POSE_LEFT_WRIST),
    (POSE_RIGHT_ELBOW, POSE_RIGHT_WRIST),
    (POSE_LEFT_SHOULDER, POSE_LEFT_HIP),
    (POSE_RIGHT_SHOULDER, POSE_RIGHT_HIP),
    (POSE_LEFT_HIP, POSE_RIGHT_HIP),
    (POSE_LEFT_HIP, POSE_LEFT_KNEE),
    (POSE_RIGHT_HIP, POSE_RIGHT_KNEE),
    (POSE_NOSE, POSE_LEFT_EAR),
    (POSE_NOSE, POSE_RIGHT_EAR),
]


# ═════════════════════════════════════════════════════════════════════════════
#  Drawing primitives
# ═════════════════════════════════════════════════════════════════════════════

def _txt(img, text, pos, color=C_WHITE, scale=0.44, thick=1, shadow=True):
    if shadow:
        cv2.putText(img, text, (pos[0]+1, pos[1]+1),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, (0,0,0), thick+2, cv2.LINE_AA)
    cv2.putText(img, text, pos,
                cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)

def _alpha(img, x, y, w, h, color, a=0.55):
    roi = img[y:y+h, x:x+w]
    if roi.shape[0]<1 or roi.shape[1]<1: return
    ov = roi.copy()
    cv2.rectangle(ov,(0,0),(w,h),color,-1)
    cv2.addWeighted(ov,a,roi,1-a,0,roi)

def _bar(img, x, y, w, h, frac, col, bg=(30,36,52)):
    frac = max(0.0,min(1.0,frac))
    cv2.rectangle(img,(x,y),(x+w,y+h),bg,-1)
    if frac > 0:
        # gradient fill
        roi = img[y:y+h, x:x+int(w*frac)]
        if roi.size > 0:
            xs = np.linspace(0,1,roi.shape[1])
            dim = np.clip(0.55+xs*0.45, 0, 1)
            for c in range(3):
                roi[:,:,c] = (col[c]*dim).astype(np.uint8)
    cv2.rectangle(img,(x,y),(x+w,y+h),C_BORDER,1)

def _score_col(v, lo=0.25, hi=0.55):
    return C_GREEN if v < lo else (C_YELLOW if v < hi else C_RED)

def _posture_label(s):
    if s < 0.20: return 'Good',   C_GREEN
    if s < 0.50: return 'Fair',   C_YELLOW
    return            'Bad',    C_RED

def _stress_label(s):
    if s < 0.20: return 'Low',    C_GREEN
    if s < 0.50: return 'Medium', C_YELLOW
    return            'High',   C_RED

def _delta_col(val, base, warn=0.08, crit=0.18):
    if val is None or base is None or base == 0: return C_GRAY
    dev = abs(val-base)/abs(base)
    return C_GREEN if dev < warn else (C_YELLOW if dev < crit else C_RED)


# ═════════════════════════════════════════════════════════════════════════════
#  Landmark Drawer — three-tier backend (Tasks → solutions → Haar)
# ═════════════════════════════════════════════════════════════════════════════

class _LandmarkDrawer:
    def __init__(self):
        self._face = self._pose = self._mp = None
        self._draw_u = self._face_conn = self._pose_conn = None
        self._cv_face = self._cv_eye = None
        self._mode = 'none'
        self.n_face = self.n_pose = 0
        self._last_face_lm  = None   # cache for smooth display
        self._last_pose_lm  = None
        self._setup()

    def _setup(self):
        try:
            import mediapipe as mp
            ver = tuple(int(x) for x in mp.__version__.split('.')[:3])
            print(f'  Drawer: mediapipe {mp.__version__}')
            if ver >= (0,10,31) and self._try_tasks(mp): return
            if self._try_solutions(mp): return
            print('  Drawer: mediapipe drawing N/A — using OpenCV Haar')
        except ImportError:
            print('  Drawer: mediapipe not installed — using OpenCV Haar')
        self._init_opencv()

    def _try_tasks(self, mp):
        try:
            from mediapipe.tasks import python as mpt
            from mediapipe.tasks.python import vision as mpv
            fp = _download_model(_FACE_MODEL_URL, 'face_landmarker.task')
            pp = _download_model(_POSE_MODEL_URL,  'pose_landmarker_lite.task')
            if not fp or not pp: return False
            self._face = mpv.FaceLandmarker.create_from_options(
                mpv.FaceLandmarkerOptions(
                    base_options=mpt.BaseOptions(model_asset_path=fp),
                    num_faces=1, min_face_detection_confidence=0.45,
                    min_face_presence_confidence=0.45, min_tracking_confidence=0.45,
                    output_face_blendshapes=False,
                    output_facial_transformation_matrixes=False))
            self._pose = mpv.PoseLandmarker.create_from_options(
                mpv.PoseLandmarkerOptions(
                    base_options=mpt.BaseOptions(model_asset_path=pp),
                    num_poses=1, min_pose_detection_confidence=0.45,
                    min_pose_presence_confidence=0.45, min_tracking_confidence=0.45))
            self._mp = mp; self._mode = 'tasks'
            print('  Drawer: Tasks API ready'); return True
        except Exception as e:
            print(f'  Drawer Tasks failed: {e}'); return False

    def _try_solutions(self, mp):
        try:
            sol = None
            for path in ['solutions','python.solutions']:
                obj = mp
                try:
                    for part in path.split('.'): obj = getattr(obj,part)
                    sol = obj; break
                except AttributeError: pass
            if sol is None:
                from mediapipe.python import solutions as sol
            self._face = sol.face_mesh.FaceMesh(
                max_num_faces=1, refine_landmarks=True,
                min_detection_confidence=0.45, min_tracking_confidence=0.45)
            self._pose = sol.pose.Pose(
                static_image_mode=False, model_complexity=2,
                enable_segmentation=False,
                min_detection_confidence=0.45, min_tracking_confidence=0.45)
            self._draw_u    = sol.drawing_utils
            self._face_conn = sol.face_mesh.FACEMESH_TESSELATION
            self._pose_conn = sol.pose.POSE_CONNECTIONS
            self._mode = 'solutions'; print('  Drawer: solutions API ready'); return True
        except Exception as e:
            print(f'  Drawer solutions failed: {e}'); return False

    def _init_opencv(self):
        try:
            self._cv_face = cv2.CascadeClassifier(
                cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
            self._cv_eye  = cv2.CascadeClassifier(
                cv2.data.haarcascades + 'haarcascade_eye.xml')
            self._mode = 'opencv'; print('  Drawer: OpenCV Haar mode')
        except Exception as e:
            print(f'  Drawer: all failed: {e}')

    # ── entry ─────────────────────────────────────────────────────────────────
    def draw(self, bgr, rgb):
        if   self._mode == 'tasks':     self._draw_tasks(bgr, rgb)
        elif self._mode == 'solutions': self._draw_solutions(bgr, rgb)
        elif self._mode == 'opencv':    self._draw_opencv(bgr)

    # ── Tasks API ─────────────────────────────────────────────────────────────
    def _draw_tasks(self, bgr, rgb):
        h,w = bgr.shape[:2]
        try:
            img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
            fr = self._face.detect(img)
            if fr and fr.face_landmarks:
                lm = fr.face_landmarks[0]
                self._last_face_lm = lm
                self.n_face = len(lm)
                self._rich_face(bgr, lm, h, w)
            else:
                self.n_face = 0
                if self._last_face_lm:   # ghost last known position (faded)
                    self._rich_face(bgr, self._last_face_lm, h, w, ghost=True)
            pr = self._pose.detect(img)
            if pr and pr.pose_landmarks:
                plm = pr.pose_landmarks[0]
                self._last_pose_lm = plm
                self.n_pose = len(plm)
                self._rich_pose(bgr, plm, h, w)
            else:
                self.n_pose = 0
                if self._last_pose_lm:
                    self._rich_pose(bgr, self._last_pose_lm, h, w, ghost=True)
        except Exception as e: print(f'  [tasks draw] {e}')

    # ── Solutions API ─────────────────────────────────────────────────────────
    def _draw_solutions(self, bgr, rgb):
        h,w = bgr.shape[:2]
        try:
            fr = self._face.process(rgb)
            if fr and fr.multi_face_landmarks:
                fl = fr.multi_face_landmarks[0]
                self._last_face_lm = fl.landmark
                self.n_face = len(fl.landmark)
                # Full tessellation mesh — faint grey web underneath features
                if self._draw_u and self._face_conn:
                    self._draw_u.draw_landmarks(bgr, fl, self._face_conn,
                        self._draw_u.DrawingSpec(color=(0,0,0),      thickness=1, circle_radius=0),
                        self._draw_u.DrawingSpec(color=(55,65,65),   thickness=1))
                self._rich_face(bgr, fl.landmark, h, w)
            else:
                self.n_face = 0
        except Exception as e:
            self.n_face = 0
        try:
            pr = self._pose.process(rgb)
            if pr and pr.pose_landmarks:
                plm = pr.pose_landmarks.landmark
                self._last_pose_lm = plm
                self.n_pose = len(plm)
                self._rich_pose(bgr, plm, h, w)
            else:
                self.n_pose = 0
        except Exception as e:
            self.n_pose = 0

    # ── Rich face overlay (used by all mediapipe backends) ────────────────────
    def _rich_face(self, bgr, lm, h, w, ghost=False):

        def fp(i):
            return int(lm[i].x*w), int(lm[i].y*h)

        def _polyline(idx_list, col, thick=1, closed=False):
            pts = np.array([fp(i) for i in idx_list], np.int32).reshape(-1,1,2)
            if ghost:
                ov = bgr.copy()
                cv2.polylines(ov, [pts], closed, col, thick, cv2.LINE_AA)
                cv2.addWeighted(ov, 0.30, bgr, 0.70, 0, bgr)
            else:
                cv2.polylines(bgr, [pts], closed, col, thick, cv2.LINE_AA)

        def _dot(i, col, r=3, ring_col=None):
            pt = fp(i)
            if ghost:
                return
            cv2.circle(bgr, pt, r, col, -1)
            if ring_col:
                cv2.circle(bgr, pt, r+1, ring_col, 1)

        a = 0.30 if ghost else 1.0

        # ── 1. All 468 landmark dots — faint mesh foundation ──────────────────
        for p in lm:
            px, py = int(p.x*w), int(p.y*h)
            if 0 <= px < w and 0 <= py < h:
                cv2.circle(bgr, (px,py), 1, (55,65,65), -1)

        # ── 2. Face silhouette — solid white closed loop ──────────────────────
        _polyline(FACE_OVAL, (210,210,210), thick=2, closed=False)

        # ── 3. Eyebrows — bright yellow, 10-pt ordered arc ───────────────────
        # Draw with a thick + thin pass to give a glowing look like the screenshot
        _polyline(BROW_L_FULL, (0,190,255), thick=3)   # thick base (orange-yellow)
        _polyline(BROW_R_FULL, (0,190,255), thick=3)
        _polyline(BROW_L_FULL, (0,230,255), thick=1)   # bright highlight on top
        _polyline(BROW_R_FULL, (0,230,255), thick=1)
        # Inner brow corner dots (key stress metric)
        _dot(BROW_L_INNER, (0,100,255), r=4, ring_col=(0,200,255))
        _dot(BROW_R_INNER, (0,100,255), r=4, ring_col=(0,200,255))
        # Outer brow tip dots
        _dot(285, (0,180,220), r=3)   # L outer tip
        _dot(55,  (0,180,220), r=3)   # R outer tip

        # ── 4. Eyes — bright green closed loops ───────────────────────────────
        _polyline(LEFT_EYE_FULL,  (0,220,60), thick=2, closed=False)
        _polyline(RIGHT_EYE_FULL, (0,220,60), thick=2, closed=False)
        # Eye corner anchor dots
        for idx in (263, 362):  _dot(idx, (0,240,80), r=3)   # left eye
        for idx in (33,  133):  _dot(idx, (0,240,80), r=3)   # right eye

        # ── 5. Iris centre — cyan dot at geometric centre of eye corners ──────
        for eye_corners in ((263,362,386,374), (33,133,160,144)):
            pts = np.array([[lm[i].x, lm[i].y] for i in eye_corners])
            cx = int(pts[:,0].mean()*w)
            cy = int(pts[:,1].mean()*h)
            cv2.circle(bgr, (cx,cy), 5, (255,220,0),  -1)   # gold iris
            cv2.circle(bgr, (cx,cy), 6, (200,240,255),  1)   # white ring

        # ── 6. Nose bridge + nostril wings ────────────────────────────────────
        _polyline(NOSE_BRIDGE, (130,140,150), thick=1)
        _polyline(NOSE_L_WING, (110,120,130), thick=1)
        _polyline(NOSE_R_WING, (110,120,130), thick=1)
        _dot(NOSE_TIP, (210,210,210), r=4)

        # ── 7. Lips — white outer, dimmer inner ───────────────────────────────
        _polyline(LIP_OUTER, (210,210,210), thick=2, closed=False)
        _polyline(LIP_INNER, (160,160,160), thick=1, closed=False)
        # Lip gap line (cyan — key stress metric for lip press)
        lu, ll = fp(LIP_UPPER), fp(LIP_LOWER)
        cv2.line(bgr, lu, ll, (255,220,40), 2, cv2.LINE_AA)
        _dot(LIP_UPPER, (255,230,80), r=3)
        _dot(LIP_LOWER, (255,230,80), r=3)
        # Mouth corner dots (orange — mouth_down signal)
        _dot(MOUTH_L_CORNER, (60,140,255), r=4, ring_col=(100,180,255))
        _dot(MOUTH_R_CORNER, (60,140,255), r=4, ring_col=(100,180,255))

        # ── 8. Forehead reference ─────────────────────────────────────────────
        _dot(FOREHEAD_TOP, (100,150,230), r=3)

    # ── Rich pose overlay ─────────────────────────────────────────────────────
    def _rich_pose(self, bgr, plm, h, w, ghost=False):
        def pp(i):
            return int(plm[i].x*w), int(plm[i].y*h)

        def vis(i):
            return getattr(plm[i], 'visibility', 1.0)

        a = 0.35 if ghost else 1.0

        # Draw all connections
        for i, j in POSE_CONNECTIONS:
            try:
                if vis(i) > 0.3 and vis(j) > 0.3:
                    p1, p2 = pp(i), pp(j)
                    if ghost:
                        ov = bgr.copy()
                        cv2.line(ov, p1, p2, C_LIME, 2, cv2.LINE_AA)
                        cv2.addWeighted(ov,0.3,bgr,0.7,0,bgr)
                    else:
                        cv2.line(bgr, p1, p2, C_LIME, 2, cv2.LINE_AA)
            except Exception: pass

        # Key-point dots with white ring
        key_pts = [
            (POSE_NOSE,           (220,220,220)),
            (POSE_LEFT_SHOULDER,  (50,150,255)),
            (POSE_RIGHT_SHOULDER, (50,150,255)),
            (POSE_LEFT_EAR,       (200,60,210)),
            (POSE_RIGHT_EAR,      (200,60,210)),
        ]
        for elbow_i in (POSE_LEFT_ELBOW, POSE_RIGHT_ELBOW):
            try:
                if vis(elbow_i) > 0.3:
                    key_pts.append((elbow_i,(80,210,80)))
            except Exception: pass

        for i, col in key_pts:
            try:
                if vis(i) > 0.3:
                    pt = pp(i)
                    if ghost:
                        pass  # skip dots in ghost mode
                    else:
                        cv2.circle(bgr, pt, 6, col,  -1)
                        cv2.circle(bgr, pt, 7, (240,240,240), 1)
            except Exception: pass

    # ── OpenCV Haar fallback ──────────────────────────────────────────────────
    def _draw_opencv(self, bgr):
        h,w = bgr.shape[:2]
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        self.n_face = self.n_pose = 0
        if self._cv_face is None: return
        faces = self._cv_face.detectMultiScale(gray,1.1,5,minSize=(60,60))
        if len(faces)==0: return
        fx,fy,fw,fh = max(faces,key=lambda r:r[2]*r[3])
        self.n_face = 468
        # Face oval approximation
        cx,cy = fx+fw//2, fy+fh//2
        cv2.ellipse(bgr,(cx,cy),(fw//2,int(fh*0.62)),0,0,360,(180,180,180),2)
        # Eyes
        ley = fy+int(fh*0.32); lew = int(fw*0.20); leh = int(fh*0.09)
        lcx = fx+int(fw*0.31); rcx = fx+int(fw*0.69)
        cv2.ellipse(bgr,(lcx,ley),(lew,leh),0,0,360,(0,220,70),2)
        cv2.ellipse(bgr,(rcx,ley),(lew,leh),0,0,360,(0,220,70),2)
        cv2.circle(bgr,(lcx,ley),4,(0,200,240),-1); cv2.circle(bgr,(rcx,ley),4,(0,200,240),-1)
        # Brows
        by_ = fy+int(fh*0.18); bw2 = int(fw*0.18)
        # curved brow approximation
        for (bcx,sign) in [(lcx,1),(rcx,-1)]:
            pts = np.array([[bcx-bw2,by_+2],[bcx,by_-3],[bcx+bw2*sign//abs(sign),by_+2]],np.int32)
            cv2.polylines(bgr,[pts],False,(30,195,230),2,cv2.LINE_AA)
        # Nose
        cv2.circle(bgr,(fx+fw//2,fy+int(fh*0.52)),5,(200,200,200),-1)
        _line_bgr = lambda p1,p2,c,t: cv2.line(bgr,p1,p2,c,t,cv2.LINE_AA)
        # Lips
        lip_cx = fx+fw//2; lip_ty = fy+int(fh*0.67); lip_by = fy+int(fh*0.77); lw2=int(fw*0.20)
        cv2.ellipse(bgr,(lip_cx,lip_ty),(lw2,int(fh*0.04)),0,0,180,(200,200,50),2)
        cv2.ellipse(bgr,(lip_cx,lip_by),(lw2,int(fh*0.05)),0,180,360,(200,200,50),2)
        cv2.line(bgr,(lip_cx,lip_ty),(lip_cx,lip_by),(255,220,0),2)
        # Shoulder estimate
        sy=fy+int(fh*1.55); shw=int(fw*1.05)
        ls_=(fx+fw//2-shw,sy); rs_=(fx+fw//2+shw,sy)
        if 0<=sy<h:
            cv2.line(bgr,ls_,rs_,(50,150,255),3,cv2.LINE_AA)
            cv2.line(bgr,(fx+int(fw*0.05),fy+int(fh*0.36)),ls_,(200,60,210),2,cv2.LINE_AA)
            cv2.line(bgr,(fx+int(fw*0.95),fy+int(fh*0.36)),rs_,(200,60,210),2,cv2.LINE_AA)
            for pt in (ls_,rs_):
                cv2.circle(bgr,pt,7,(50,150,255),-1); cv2.circle(bgr,pt,8,(255,255,255),1)
        _txt(bgr,'Haar approx',(fx,fy-6),(80,175,80),scale=0.34)

    def close(self):
        for obj in (self._face, self._pose):
            if obj:
                try: obj.close()
                except Exception: pass


# ═════════════════════════════════════════════════════════════════════════════
#  Spine angle from live metrics (same formula as real posture logic)
# ═════════════════════════════════════════════════════════════════════════════

def _spine_angle_deg(live):
    sf = live.get('shoulder_forward')
    if sf is None: return None
    return round(90.0 - math.degrees(math.atan2(sf, 0.30)), 1)


# ═════════════════════════════════════════════════════════════════════════════
#  HUD bar (top strip)
# ═════════════════════════════════════════════════════════════════════════════

HUD_H = 38

def _draw_hud(row, live, fps, W):
    cv2.rectangle(row,(0,0),(W,HUD_H),C_HUD,-1)
    cv2.line(row,(0,HUD_H-1),(W,HUD_H-1),C_BORDER,1)

    ps   = live.get('posture_score',0.0)
    ss   = live.get('stress_score', 0.0)
    ear  = live.get('eye_ear')
    bpm  = live.get('blink_rate', 0)
    cal  = live.get('calibrated', False)

    pl,pc = _posture_label(ps)
    sl,sc = _stress_label(ss)

    y = HUD_H-10
    _txt(row,'Posture:',(10,y),C_LGRAY,scale=0.44)
    _txt(row,pl,        (80,y),pc,      scale=0.44,thick=2)

    _txt(row,'Stress:', (180,y),C_LGRAY,scale=0.44)
    _txt(row,sl,        (245,y),sc,     scale=0.44,thick=2)

    ear_s = f'{ear:.2f}' if ear is not None else '--'
    _txt(row,'EAR:',    (345,y),C_LGRAY,scale=0.44)
    _txt(row,ear_s,     (385,y),C_WHITE,scale=0.44)

    bpm_s = f'{bpm*2:.1f}'   # blink_rate is per-30s; ×2 → per-min
    _txt(row,'Blinks/min:',(460,y),C_LGRAY,scale=0.44)
    _txt(row,bpm_s,         (555,y),C_WHITE,scale=0.44)

    if not cal:
        pct = live.get('calib_pct',0)
        _txt(row,f'CALIBRATING  {pct:.0f}%',(W//2-75,y),C_YELLOW,scale=0.42,thick=2)

    fps_col = C_GREEN if fps>=20 else (C_YELLOW if fps>=12 else C_RED)
    _txt(row,f'FPS: {fps:.1f}',(W-80,y),fps_col,scale=0.42)


# ═════════════════════════════════════════════════════════════════════════════
#  Alert badge (top-right corner)
# ═════════════════════════════════════════════════════════════════════════════

# Human-readable signal labels matching the screenshot style
_SIG_LABELS = {
    'blink_low':     'Low blinks',
    'blink_high':    'High blinks',
    'eye_narrow':    'Eye strain',
    'brow_contract': 'Brow tension',
    'brow_lower':    'Brow lowered',
    'lip_press':     'Pressed lips',
    'mouth_down':    'Mouth down',
    'forward_head':  'Head forward',
    'head_droop':    'Head droop',
    'rounded_shld':  'Rounded shoulders',
    'elevated_shld': 'Elevated shoulders',
    'tech_neck':     'Tech neck',
}

# Sit-straight coaching messages per signal
_COACHING = {
    'rounded_shld':  'Sit Straight',
    'elevated_shld': 'Drop shoulders',
    'tech_neck':     'Chin back',
    'forward_head':  'Sit back',
    'head_droop':    'Lift your head',
    'brow_contract': 'Relax brows',
    'eye_narrow':    'Rest your eyes',
    'lip_press':     'Unclench jaw',
    'blink_low':     'Remember to blink',
    'blink_high':    'Take a breath',
    'brow_lower':    'Soften face',
    'mouth_down':    'Ease tension',
}

def _draw_alert_badge(frame, live):
    h,w = frame.shape[:2]
    active = live.get('active_signals',[])
    cal    = live.get('calibrated',False)
    if not cal: return

    if not active:
        # Green "good" badge
        badge_w, badge_h = 180, 36
        bx = w - badge_w - 8; by = 8
        _alpha(frame,bx,by,badge_w,badge_h,(10,60,10),a=0.80)
        cv2.rectangle(frame,(bx,by),(bx+badge_w,by+badge_h),(0,180,50),2)
        cv2.circle(frame,(bx+14,by+badge_h//2),7,(0,200,60),-1)
        _txt(frame,'Good posture',(bx+26,by+badge_h//2+5),C_GREEN,scale=0.44,thick=2)
        return

    sig = active[0]
    is_s = sig in StressPostureDetector.STRESS_SIGNALS
    coaching = _COACHING.get(sig,'')
    detail   = _SIG_LABELS.get(sig, sig.replace('_',' '))

    # Extra context lines for the badge
    extra = []
    if len(active) > 1:
        for s2 in active[1:3]:
            extra.append(_SIG_LABELS.get(s2, s2.replace('_',' ')))

    lines = [coaching, detail] + extra
    lines = [l for l in lines if l]

    badge_w = 220
    badge_h = 16 + len(lines)*20 + 4
    bx = w - badge_w - 8; by = 8

    box_col  = (12,12,70) if is_s else (12,20,70)
    ring_col = C_YELLOW   if is_s else C_RED

    _alpha(frame,bx,by,badge_w,badge_h,box_col,a=0.85)
    cv2.rectangle(frame,(bx,by),(bx+badge_w,by+badge_h),ring_col,2)

    # Red/yellow dot indicator
    dot_col = ring_col
    cv2.circle(frame,(bx+12,by+14),6,dot_col,-1)

    for li,line in enumerate(lines):
        scale = 0.46 if li==0 else 0.38
        col   = ring_col if li==0 else C_LGRAY
        thick = 2 if li==0 else 1
        _txt(frame,line,(bx+26, by+16+li*20),col,scale=scale,thick=thick)


# ═════════════════════════════════════════════════════════════════════════════
#  Posture geometry overlay on frame (spine/head/neck lines)
# ═════════════════════════════════════════════════════════════════════════════

def _posture_lines_on_frame(frame, live):
    """Draw spine angle, head drop and neck forward metrics directly on video."""
    h,w = frame.shape[:2]
    # These are computed values, not pixel positions — show as text overlay
    # positioned over the lower torso area
    sf   = live.get('shoulder_forward')
    hy   = live.get('head_y')
    esz  = live.get('ear_shoulder_z')
    ang  = _spine_angle_deg(live)

    y0 = int(h * 0.66)
    x0 = 8

    def metric_line(label, val_s, col, y):
        _txt(frame, label, (x0, y),     C_LGRAY, scale=0.40, shadow=True)
        _txt(frame, val_s, (x0+130, y), col,     scale=0.40, shadow=True)

    ang_col = (C_GREEN if ang is not None and ang>82
               else C_YELLOW if ang is not None and ang>72
               else C_RED if ang is not None else C_GRAY)
    hy_col  = _delta_col(hy,  live.get('__baseline_head_y'),  0.06, 0.12)
    sf_col  = _delta_col(sf,  live.get('__baseline_shld_fwd'),0.07, 0.14)

    metric_line('Spine angle:',   f'{ang:.1f} deg'  if ang  is not None else '—', ang_col, y0)
    metric_line('Head drop:',     f'{hy:+.3f}'       if hy   is not None else '—', hy_col,  y0+20)
    metric_line('Neck forward:',  f'{esz:.3f}'       if esz  is not None else '—', sf_col,  y0+40)


# ═════════════════════════════════════════════════════════════════════════════
#  Stress monitor panel (bottom-left, mirrors the screenshot exactly)
# ═════════════════════════════════════════════════════════════════════════════

STRESS_PANEL_W = 440
STRESS_PANEL_H = 160

# Emotion states inferred from active stress signals
_EMOTION_STATES = {
    frozenset(): ('NEUTRAL', ':-|', C_WHITE),
    frozenset(['brow_contract','lip_press']): ('ANGRY',    '>:-|', C_RED),
    frozenset(['brow_contract']):             ('TENSE',    ':-/', C_YELLOW),
    frozenset(['eye_narrow','brow_lower']):   ('TIRED',    ':-z', C_YELLOW),
    frozenset(['blink_low']):                 ('FOCUSED',  '8-|', C_CYAN),
    frozenset(['lip_press']):                 ('STRESSED', ':-|', C_ORANGE),
    frozenset(['mouth_down']):                ('SAD',      ':-(', C_BLUE),
    frozenset(['blink_high']):                ('ANXIOUS',  'O_O', C_YELLOW),
}

def _emotion_from_signals(active):
    stress_active = set(s for s in active if s in StressPostureDetector.STRESS_SIGNALS)
    # Best match: largest frozenset that is a subset of active
    best_key = frozenset()
    best_label = ('NEUTRAL',':-|',C_WHITE)
    for key, label in _EMOTION_STATES.items():
        if key and key.issubset(stress_active) and len(key) > len(best_key):
            best_key = key
            best_label = label
    if not stress_active:
        return 'NEUTRAL',':-|',C_WHITE
    return best_label

def _signal_confidence(sig, live, baseline):
    """Return 0-100% confidence that this signal is real, based on deviation magnitude."""
    key_map = {
        'brow_contract': 'brow_inner_dist',
        'brow_lower':    'brow_y',
        'eye_narrow':    'eye_ear',
        'lip_press':     'lip_gap',
        'mouth_down':    'mouth_corner_delta',
        'forward_head':  'face_size',
        'head_droop':    'head_y',
        'rounded_shld':  'shoulder_forward',
        'elevated_shld': 'shoulder_y',
        'tech_neck':     'ear_shoulder_z',
    }
    key = key_map.get(sig)
    if key is None: return 35
    val = live.get(key); base = baseline.get(key)
    if val is None or base is None or base == 0: return 35
    dev = abs(val-base)/abs(base)
    return min(int(dev * 300), 99)

def _draw_stress_panel(frame, live, baseline):
    h,w = frame.shape[:2]
    px = 0
    py = h - STRESS_PANEL_H
    _alpha(frame, px, py, STRESS_PANEL_W, STRESS_PANEL_H, C_PANEL, a=0.82)
    cv2.rectangle(frame,(px,py),(px+STRESS_PANEL_W,py+STRESS_PANEL_H),C_BORDER,1)
    cv2.line(frame,(px,py),(px+STRESS_PANEL_W,py),C_BORDER,1)

    active  = live.get('active_signals',[])
    ss      = live.get('stress_score',0.0)
    ps      = live.get('posture_score',0.0)
    cal     = live.get('calibrated',False)

    emo_label, emo_face, emo_col = _emotion_from_signals(active)
    sl,sc = _stress_label(ss)
    pl,pc = _posture_label(ps)

    cy = py + 18
    # Title row
    _txt(frame,'STRESS MONITOR',(px+8,cy),C_LGRAY,scale=0.42,thick=1)
    cy += 22

    # Emotion + face + stress level — row 2
    _txt(frame, emo_label,  (px+8,  cy), emo_col, scale=0.68, thick=2)
    _txt(frame, emo_face,   (px+150,cy), emo_col, scale=0.60, thick=2)
    _txt(frame, f'Stress: {sl}', (px+230,cy), sc, scale=0.44)
    cy += 26

    # Anger/stress bar with label
    stress_active = [s for s in active if s in StressPostureDetector.STRESS_SIGNALS]
    # "ANGRY" bar — driven by lip_press + brow_contract presence
    anger_score = 0.0
    if 'lip_press'     in active: anger_score += 0.5
    if 'brow_contract' in active: anger_score += 0.5
    if 'brow_lower'    in active: anger_score += 0.3
    if 'mouth_down'    in active: anger_score += 0.3
    anger_score = min(anger_score, 1.0)

    bar_label = 'ANGRY'   if anger_score > 0.4 else ('TENSE' if ss > 0.2 else 'CALM')
    bar_col   = C_RED     if anger_score > 0.4 else (C_YELLOW if ss > 0.2 else C_GREEN)
    _txt(frame, bar_label, (px+8, cy+12), bar_col, scale=0.38, thick=1)
    _bar(frame, px+74, cy+2, 280, 16, max(anger_score, ss), bar_col)
    _txt(frame, f'{max(anger_score,ss)*5:.1f}', (px+362,cy+12), C_LGRAY, scale=0.36)
    cy += 26

    # Signals detected row
    _txt(frame,'Signals detected:',(px+8,cy),C_LGRAY,scale=0.36)
    cy += 16

    if not cal:
        _txt(frame,'Calibrating…',(px+8,cy),C_YELLOW,scale=0.36)
        cy += 16
    elif not active:
        _txt(frame,'None — all clear',(px+8,cy),C_GREEN,scale=0.36)
        cy += 16
    else:
        for sig in active[:3]:
            is_s  = sig in StressPostureDetector.STRESS_SIGNALS
            col   = C_YELLOW if is_s else C_RED
            conf  = _signal_confidence(sig, live, baseline)
            label = _SIG_LABELS.get(sig, sig.replace('_',' '))
            dot_col = col
            cv2.circle(frame,(px+12,cy-4),5,dot_col,-1)
            _txt(frame, f'{label.capitalize()} ({conf}%)', (px+22,cy), col, scale=0.35)
            cy += 14
            if cy >= py+STRESS_PANEL_H-10: break

    # Bottom row: raw/smoothed posture
    cy = py + STRESS_PANEL_H - 16
    _txt(frame,f'Raw posture: {pl}',(px+8,cy),pc,scale=0.34)
    _txt(frame,f'Smoothed: {pl}',(px+180,cy),pc,scale=0.34)
    mode = live.get('detection_mode','opencv')
    mode_col = C_GREEN if mode=='legacy' else (C_YELLOW if mode=='tasks' else C_GRAY)
    _txt(frame,f'Voice: {"ON" if cal else "CALIB"}',(px+340,cy),mode_col,scale=0.34)


# ═════════════════════════════════════════════════════════════════════════════
#  Calibration overlay
# ═════════════════════════════════════════════════════════════════════════════

def _calib_overlay(frame, pct):
    h,w = frame.shape[:2]
    _alpha(frame,0,0,w,h,(0,0,0),a=0.40)
    bw=int(w*0.50); bh=28; bx=(w-bw)//2; by=h//2-60
    _txt(frame,'CALIBRATING — sit naturally, look at screen',(bx-10,by-16),C_YELLOW,scale=0.52,thick=2)
    _txt(frame,'Building your personal baseline for accurate stress/posture detection…',(bx-10,by+0),C_WHITE,scale=0.36)
    cv2.rectangle(frame,(bx,by+10),(bx+bw,by+10+bh),(20,20,20),-1)
    fill=int(bw*pct/100)
    if fill>0:
        roi=frame[by+10:by+10+bh,bx:bx+fill]
        if roi.size>0:
            xs=np.linspace(0,1,roi.shape[1])
            roi[:]=np.stack([(40+xs*160).astype(np.uint8),
                              (170+xs*45).astype(np.uint8),
                              (45+xs*35).astype(np.uint8)],axis=1)[np.newaxis]
    cv2.rectangle(frame,(bx,by+10),(bx+bw,by+10+bh),C_BORDER,1)
    _txt(frame,f'{pct:.0f}%',(bx+bw+10,by+10+20),C_YELLOW,scale=0.46)
    _txt(frame,'Press  C  to skip',(bx+bw//2-55,by+10+bh+18),C_GRAY,scale=0.34)


# ═════════════════════════════════════════════════════════════════════════════
#  Detector init without camera thread
# ═════════════════════════════════════════════════════════════════════════════

def _init_no_thread(det: StressPostureDetector):
    det.session            = SessionData(start_time=time.time())
    det._calib_start       = time.time()
    det._calib_done        = False
    for k in det._calib:           det._calib[k].clear()
    for k in det._baseline:        det._baseline[k] = None
    for k in det._sustained_since: det._sustained_since[k] = None
    for hh in det._hist.values():  hh.clear()
    det._last_alert        = {k: 0.0 for k in det.COOLDOWNS}
    det._good_streak_start = None
    det._last_calib_t      = 0.0
    det._last_hist_t       = 0.0
    det._running           = True
    det._frame_n           = 0


# ═════════════════════════════════════════════════════════════════════════════
#  Main
# ═════════════════════════════════════════════════════════════════════════════

def run(cam_index=0):
    print('╔══════════════════════════════════════════════════════════╗')
    print('║  Stress & Posture — Real-Time Debug View                ║')
    print('╠══════════════════════════════════════════════════════════╣')
    print('║  Q/ESC=quit   C=skip calibration   F=fullscreen         ║')
    print('╚══════════════════════════════════════════════════════════╝')

    det = StressPostureDetector(on_alert=None)
    _init_no_thread(det)
    print(f'  Detector  : {det.detection_mode}')

    drawer = _LandmarkDrawer()
    print(f'  Drawer    : {drawer._mode}')

    cap = None
    for idx in [cam_index]+[i for i in range(4) if i!=cam_index]:
        c = cv2.VideoCapture(idx)
        if c.isOpened():
            cap=c; print(f'  Camera    : {idx}'); break
    if cap is None:
        print('ERROR: no camera'); return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    cap.set(cv2.CAP_PROP_FPS, 30)
    # Read back actual size (some cams don't support 1280×720)
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f'  Resolution: {actual_w}×{actual_h}')

    fps_buf: Deque[float] = collections.deque(maxlen=60)
    fullscreen = False

    WIN = 'Stress & Posture — Debug View'
    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, actual_w, actual_h + HUD_H)

    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.02); continue

        frame = cv2.flip(frame, 1)
        rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Feed detector every other frame (matches _loop cadence)
        det._frame_n += 1
        if det._frame_n % 2 == 0:
            det._process(frame)

        # Draw full mesh + skeleton onto the frame
        drawer.draw(frame, rgb)

        # Pull live state and baseline snapshot
        live     = det.get_live()
        baseline = dict(det._baseline)

        # Inject baseline hints so posture_lines_on_frame can colour-code
        live['__baseline_head_y']  = baseline.get('head_y')
        live['__baseline_shld_fwd']= baseline.get('shoulder_forward')

        # FPS
        fps_buf.append(time.time())
        fps = ((len(fps_buf)-1)/max(fps_buf[-1]-fps_buf[0],1e-6)
               if len(fps_buf)>=2 else 0.0)

        # ── overlays directly on camera frame ────────────────────────────
        if not live.get('calibrated',False):
            _calib_overlay(frame, live.get('calib_pct',0))
        else:
            _posture_lines_on_frame(frame, live)
            _draw_stress_panel(frame, live, baseline)

        _draw_alert_badge(frame, live)

        # ── HUD bar stacked above frame ───────────────────────────────────
        fh,fw = frame.shape[:2]
        hud = np.full((HUD_H,fw,3), C_HUD, dtype=np.uint8)
        _draw_hud(hud, live, fps, fw)

        output = np.vstack([hud, frame])
        cv2.imshow(WIN, output)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord('q'),ord('Q'),27): break
        if key in (ord('c'),ord('C')) and not det._calib_done:
            det._finalise_calibration()
            print('  [C] Calibration skipped.')
        if key in (ord('f'),ord('F')):
            fullscreen = not fullscreen
            flag = cv2.WINDOW_FULLSCREEN if fullscreen else cv2.WINDOW_NORMAL
            cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN,
                                  cv2.WINDOW_FULLSCREEN if fullscreen else cv2.WINDOW_NORMAL)

    det._running = False
    det.session.end_time = time.time()
    det._backend.close()
    drawer.close()
    cap.release()
    cv2.destroyAllWindows()
    print('\n  Done.')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Real-time debug view')
    ap.add_argument('--cam', type=int, default=0)
    run(cam_index=ap.parse_args().cam)
