"""
Horizon Mode-specific gaze/head-pose monitor.

Deliberately kept separate from core/detector.py — this is its own
component with its own MediaPipe FaceMesh instance, own landmark math,
and own state. It does not touch or depend on the main stress/posture
detection pipeline, and the main detector doesn't know anything about
"looking at screen" — it only optionally hands raw frames over via
`StressPostureDetector.frame_hook` while Horizon Mode is running (see
tray_app.py). Nothing here runs unless Horizon Mode is active.

Why a separate FaceMesh instance rather than reusing the shared one:
the shared backend in detector.py is created with `refine_landmarks=
False` (no iris landmarks), because the main stress/posture signals
don't need them. Real gaze direction does need iris position, so this
module creates its own FaceMesh with `refine_landmarks=True`. Frames
are still read from the SAME physical camera the main detector already
owns (via the hook) — this does NOT open a second camera device, which
would fail on most hardware/OSes if attempted simultaneously.

"Looking at screen" is True only when ALL of the following hold:
  - a face is detected
  - eyes are open (EAR-based, same technique as core/detector.py's
    blink detection, same landmark indices for consistency)
  - gaze direction is approximately forward (iris position relative to
    each eye's own corners — a standard ratio-based approximation, not
    clinical eye tracking)
  - head is approximately facing the camera (solvePnP 6-point head
    pose estimation against a generic average face model — the
    standard OpenCV technique for this; it is NOT a measurement of
    this specific user's actual face geometry, just industry-standard
    approximate head pose)

Critically: the per-frame (instantaneous) result above is NEVER used
directly. It has to stay consistent for TRANSITION_SEC (default 0.75s)
before the CONFIRMED state (`looking_at_screen`) actually changes. This
is what prevents a single noisy/misread frame from flipping the state —
the bug where the very first frame (captured the instant Horizon Mode
opens, while the user is still looking at the screen they just clicked
on) fired a notification immediately was exactly this class of problem.
"""

import os
import sys
import time
import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks.python import vision as mp_vision
from mediapipe.tasks.python.core.base_options import BaseOptions
from mediapipe.tasks.python.vision.core.vision_task_running_mode import (
    VisionTaskRunningMode,
)

# MediaPipe >=1.0 dropped the legacy `mp.solutions.face_mesh` API in
# favor of the Tasks API (`mediapipe.tasks.python.vision`). This is
# purely an API-surface change on MediaPipe's side, not a capability
# change relevant here: the Tasks API's FaceLandmarker uses the same
# 478-point face mesh topology as the old solution (468 base points +
# 10 iris points at the same indices, 469-477), confirmed against the
# installed package's own `FaceLandmarksConnections.FACE_LANDMARKS_
# LEFT_IRIS` / `..._RIGHT_IRIS` constants. Unlike the old API, iris
# points are always included in Tasks API output (no
# `refine_landmarks` flag) — the face_landmarker.task model bundle
# always outputs all 478 landmarks. So every landmark index this
# module already uses (_EYE_A/_EYE_B/_IRIS_A/_IRIS_B/_POSE_LANDMARK_IDX)
# is unchanged, and NormalizedLandmark objects still expose .x/.y/.z,
# so _eye_aspect_ratio / _gaze_forward / _head_forward need no changes.
#
# What DOES change: FaceLandmarker requires a local model bundle file
# (not bundled with the pip package). Download it once:
#   https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
# and place it at ui/horizon/models/face_landmarker.task (next to this
# file), or point _MODEL_PATH below at wherever you keep it.
_MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "face_landmarker.task")


# Same landmark indices used by core/detector.py's blink/EAR detection,
# reused here deliberately for consistency between the two components
# rather than picking different ones.
_EYE_A = (263, 362, 386, 374)   # outer, inner, top, bottom
_EYE_B = (33,  133, 160, 144)   # outer, inner, top, bottom

# Iris landmarks (indices 468-477 beyond the canonical 468-point face
# mesh; always present in Tasks API output, see note above import).
# Using the mean of
# each eye's 4 iris boundary points as the iris "center" is more robust
# than trusting a single index as the center, regardless of minor
# indexing convention differences between references.
_IRIS_A = (474, 475, 476, 477)
_IRIS_B = (469, 470, 471, 472)

# 6-point generic average adult face model (arbitrary units) for
# solvePnP head-pose estimation — the standard technique (see e.g. any
# "head pose estimation with OpenCV + face landmarks" reference); this
# is an approximate average face, not a measurement of the actual user.
_MODEL_POINTS_3D = np.array([
    (0.0,    0.0,    0.0),     # nose tip      (landmark 1)
    (0.0,  -63.6,  -12.5),     # chin          (landmark 152)
    (-43.3,  32.7,  -26.0),    # eye corner A  (landmark 33)
    (43.3,   32.7,  -26.0),    # eye corner B  (landmark 263)
    (-28.9, -28.9,  -24.1),    # mouth corner A(landmark 61)
    (28.9,  -28.9,  -24.1),    # mouth corner B(landmark 291)
], dtype=np.float64)
_POSE_LANDMARK_IDX = (1, 152, 33, 263, 61, 291)

EAR_CLOSED_THRESHOLD = 0.15   # below this, sustained, counts as "eyes closed"
                              # (core/detector.py's instantaneous blink-crossing
                              # threshold is 0.20 — this is deliberately a bit
                              # looser so a normal blink isn't read as "closed")
GAZE_FORWARD_RANGE = (0.35, 0.65)   # iris position ratio considered "forward", per axis
HEAD_YAW_MAX_DEG   = 20.0           # max left/right head turn considered "facing camera"
HEAD_PITCH_MAX_DEG = 20.0           # max up/down head tilt considered "facing camera"


def _eye_aspect_ratio(lm, idx):
    pts = np.array([[lm[i].x, lm[i].y] for i in idx])
    h = np.linalg.norm(pts[2] - pts[3])
    w = np.linalg.norm(pts[0] - pts[1])
    return h / w if w > 1e-6 else 0.0


class HorizonGazeMonitor:
    """
    Lives only as long as Horizon Mode is showing. Created fresh each
    time, closed when the break ends — not a shared/global singleton.
    """

    TRANSITION_SEC = 0.75   # sustained duration required before the
                             # CONFIRMED state is allowed to change —
                             # this is the "don't trigger on one frame"
                             # requirement.

    def __init__(self):
        if not os.path.isfile(_MODEL_PATH):
            raise FileNotFoundError(
                f"FaceLandmarker model not found at {_MODEL_PATH}. Download it "
                "from https://storage.googleapis.com/mediapipe-models/"
                "face_landmarker/face_landmarker/float16/1/face_landmarker.task "
                "and place it at that path."
            )
        options = mp_vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=_MODEL_PATH),
            running_mode=VisionTaskRunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._face_mesh = mp_vision.FaceLandmarker.create_from_options(options)
        # VIDEO mode timestamps must be monotonically increasing for the
        # lifetime of this FaceLandmarker instance (per-call, not
        # wall-clock). Using elapsed-ms-since-construction rather than
        # raw time.time()*1000 keeps this safe even across daylight
        # saving / clock adjustments, and _last_ts_ms below guards
        # against two frames landing on the same millisecond.
        self._t0 = time.time()
        self._last_ts_ms = -1

        # Opt-in diagnostic logging (off by default — set
        # CANARY_HORIZON_DEBUG=1 in the environment to enable). Prints
        # the actual computed ear/gx/gy/yaw/pitch values and which
        # individual condition passed/failed, rate-limited to ~2/sec,
        # so a real failure can be diagnosed from real numbers instead
        # of guessing at threshold changes.
        self._debug = os.environ.get('CANARY_HORIZON_DEBUG') == '1'
        self._debug_last_print = 0.0
        self._last_ear = self._last_gx = self._last_gy = None
        self._last_yaw = self._last_pitch = None
        # Fail-safe defaults: start (and fall back, on any error or
        # missing face) to "not looking at screen" rather than the
        # opposite. A missed reminder is a minor inconvenience; a
        # spurious one — like the immediate-on-open bug — is the
        # actual problem being fixed here, so uncertainty should never
        # resolve toward firing a notification.
        self.looking_at_screen = False
        self._pending_state = False
        self._pending_since = None

    def process_frame(self, bgr_frame: np.ndarray) -> bool:
        """
        Called once per camera frame (via StressPostureDetector's
        optional frame_hook) while Horizon Mode is active. Returns the
        current CONFIRMED (debounced) state — same value as reading
        self.looking_at_screen afterward.
        """
        instantaneous = self._evaluate(bgr_frame)
        now = time.time()

        if instantaneous == self._pending_state:
            if self._pending_since is None:
                self._pending_since = now
            elif now - self._pending_since >= self.TRANSITION_SEC:
                self.looking_at_screen = instantaneous
        else:
            self._pending_state = instantaneous
            self._pending_since = now

        return self.looking_at_screen

    def close(self):
        try:
            self._face_mesh.close()
        except Exception:
            pass

    # ── Internals ────────────────────────────────────────────────────

    def _evaluate(self, bgr_frame: np.ndarray) -> bool:
        try:
            h, w = bgr_frame.shape[:2]
            rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            timestamp_ms = int((time.time() - self._t0) * 1000)
            if timestamp_ms <= self._last_ts_ms:
                timestamp_ms = self._last_ts_ms + 1
            self._last_ts_ms = timestamp_ms
            result = self._face_mesh.detect_for_video(mp_image, timestamp_ms)
            if not result.face_landmarks:
                if self._debug:
                    self._debug_print("no face detected")
                return False
            lm = result.face_landmarks[0]

            eyes_ok = self._eyes_open(lm)
            gaze_ok = self._gaze_forward(lm)
            head_ok = self._head_forward(lm, w, h)

            if self._debug:
                self._debug_print(
                    f"ear={self._last_ear:.3f}(>{EAR_CLOSED_THRESHOLD}) "
                    f"gx={self._last_gx:.3f} gy={self._last_gy:.3f} "
                    f"(range {GAZE_FORWARD_RANGE}) "
                    f"yaw={self._last_yaw:.1f} pitch={self._last_pitch:.1f} "
                    f"(max ±{HEAD_YAW_MAX_DEG}/±{HEAD_PITCH_MAX_DEG}) "
                    f"| eyes_ok={eyes_ok} gaze_ok={gaze_ok} head_ok={head_ok} "
                    f"-> looking={eyes_ok and gaze_ok and head_ok}"
                )

            return eyes_ok and gaze_ok and head_ok
        except Exception as e:
            if self._debug:
                self._debug_print(f"EXCEPTION in _evaluate: {e!r}")
            # Any unexpected error (bad frame, landmark math edge case,
            # etc.) resolves to "not looking at screen" — see the
            # fail-safe reasoning in __init__.
            return False

    def _debug_print(self, msg: str):
        now = time.time()
        if now - self._debug_last_print >= 0.5:
            self._debug_last_print = now
            print(f"[horizon_gaze] {msg}", file=sys.stderr)

    def _eyes_open(self, lm) -> bool:
        ear = (_eye_aspect_ratio(lm, _EYE_A) + _eye_aspect_ratio(lm, _EYE_B)) / 2.0
        if self._debug:
            self._last_ear = ear
        return ear > EAR_CLOSED_THRESHOLD

    def _gaze_forward(self, lm) -> bool:
        def ratio_for(eye_idx, iris_idx):
            outer, inner, top, bottom = (np.array([lm[i].x, lm[i].y]) for i in eye_idx)
            iris_pts = np.array([[lm[i].x, lm[i].y] for i in iris_idx])
            iris_c = iris_pts.mean(axis=0)
            ex0, ex1 = sorted([outer[0], inner[0]])
            ey0, ey1 = sorted([top[1], bottom[1]])
            gx = (iris_c[0] - ex0) / (ex1 - ex0) if (ex1 - ex0) > 1e-6 else 0.5
            gy = (iris_c[1] - ey0) / (ey1 - ey0) if (ey1 - ey0) > 1e-6 else 0.5
            return gx, gy

        gx_a, gy_a = ratio_for(_EYE_A, _IRIS_A)
        gx_b, gy_b = ratio_for(_EYE_B, _IRIS_B)
        gx = (gx_a + gx_b) / 2.0
        gy = (gy_a + gy_b) / 2.0

        if self._debug:
            self._last_gx, self._last_gy = gx, gy

        lo, hi = GAZE_FORWARD_RANGE
        return (lo <= gx <= hi) and (lo <= gy <= hi)

    def _head_forward(self, lm, w: int, h: int) -> bool:
        image_points = np.array(
            [[lm[i].x * w, lm[i].y * h] for i in _POSE_LANDMARK_IDX],
            dtype=np.float64,
        )
        focal_length = w
        center = (w / 2, h / 2)
        camera_matrix = np.array([
            [focal_length, 0, center[0]],
            [0, focal_length, center[1]],
            [0, 0, 1],
        ], dtype=np.float64)
        dist_coeffs = np.zeros((4, 1))

        ok, rvec, _ = cv2.solvePnP(
            _MODEL_POINTS_3D, image_points, camera_matrix, dist_coeffs,
            flags=cv2.SOLVEPNP_ITERATIVE,
        )
        if not ok:
            return False

        rmat, _ = cv2.Rodrigues(rvec)
        angles, _, _, _, _, _ = cv2.RQDecomp3x3(rmat)
        pitch, yaw, _roll = angles

        # RQDecomp3x3 has a known ambiguity: for a face roughly facing the
        # camera, it can return the mathematically-equivalent "flipped"
        # solution (pitch mirrored by ~180 degrees) instead of the
        # expected near-zero value, even though the underlying rotation is
        # identical. Confirmed against logged output: yaw was already
        # correct (~0 deg, facing the camera dead-on) while pitch was
        # consistently landing at ~+-179 deg on every frame instead of
        # ~0 deg. Standard correction: mirror pitch back to its stable
        # equivalent whenever it exceeds +-90 deg.
        if pitch > 90:
            pitch = 180 - pitch
        elif pitch < -90:
            pitch = -180 - pitch

        if self._debug:
            self._last_yaw, self._last_pitch = yaw, pitch

        return abs(yaw) <= HEAD_YAW_MAX_DEG and abs(pitch) <= HEAD_PITCH_MAX_DEG