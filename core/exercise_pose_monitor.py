"""
core/exercise_pose_monitor.py — Unplug Mode: per-exercise pose validation
═══════════════════════════════════════════════════════════════════════════
Deliberately separate from core/detector.py, same reasoning as
ui/horizon/horizon_gaze.py: this is its own component with its own
MediaPipe model instances, own landmark math, and own state. It does
not touch or modify the main stress/posture pipeline. It reads frames
from the SAME physical camera the main detector (or a locally-owned
one, see ui/unplug/exercise_validation_coordinator.py) already has
open, via the existing `StressPostureDetector.frame_hook` extension
point — this does NOT open a second camera device.

Scope — which exercises this can actually validate
----------------------------------------------------
Five of the seven authored exercises have a geometric signal that is
reliably readable from face/pose landmarks with a normal webcam:

    head_tilt      → head roll angle
    head_nod       → head pitch angle
    head_rotation  → head yaw angle
    head_look      → iris/gaze offset (eyes move, head stays forward)
    arm_raise      → wrist raised above shoulder line

Two exercises are NOT validated here, on purpose:

    leg_raise      → lower body is rarely framed by a webcam pointed
                     at a face/upper-body, and BlazePose leg landmarks
                     are unreliable at typical desk-camera angles/crop.
    heel_raise     → same camera-framing problem, plus the motion
                     (heel lift) is too small to distinguish reliably
                     from normal foot micro-movement at this landmark
                     resolution.

Callers must NOT invoke evaluate() for those two ids — they are
expected to use a timed/predetermined progression instead (see
ExerciseValidationCoordinator). This module doesn't silently pretend
to validate them; it raises if asked to, so a call-site bug is caught
immediately rather than quietly reporting fake "correct" results.

Degrades honestly
------------------
If MediaPipe's Tasks API or model download isn't available, every
evaluate() call returns None ("inconclusive") forever — it never
raises out of the camera thread and never fabricates a verdict. Every
caller (see _AttemptTracker below) already treats None as "no signal
yet" and times out to "proceed anyway" like any other camera-absent
case. So a machine with no internet for the one-time model download,
or an old mediapipe version, degrades to the same graceful
timed-fallback behavior as a missing camera — never a crash, never a
fake pass.
"""

import time
from dataclasses import dataclass
from typing import Optional, Dict, Tuple

import cv2
import numpy as np

# Reusing the same Tasks-API model URLs + local-cache downloader that
# core/detector.py already uses for its own optional Tasks-API path —
# same cache directory (~/.stress_posture_models/), so if detector.py
# already downloaded the pose model for its own use, this reuses that
# exact file rather than fetching a second copy.
from core.detector import _download_model, _FACE_MODEL_URL, _POSE_MODEL_URL


# ─────────────────────────────────────────────────────────────────────────
#  Landmark indices — same ones ui/horizon/horizon_gaze.py uses, kept
#  local here (core/ doesn't import from ui/) rather than shared, so
#  the two components stay independently modifiable as their own
#  docstrings already establish.
# ─────────────────────────────────────────────────────────────────────────

_EYE_A = (263, 362, 386, 374)   # outer, inner, top, bottom
_EYE_B = (33,  133, 160, 144)   # outer, inner, top, bottom
_IRIS_A = (474, 475, 476, 477)
_IRIS_B = (469, 470, 471, 472)

_MODEL_POINTS_3D = np.array([
    (0.0,    0.0,    0.0),     # nose tip       (landmark 1)
    (0.0,  -63.6,  -12.5),     # chin           (landmark 152)
    (-43.3,  32.7,  -26.0),    # eye corner A   (landmark 33)
    (43.3,   32.7,  -26.0),    # eye corner B   (landmark 263)
    (-28.9, -28.9,  -24.1),    # mouth corner A (landmark 61)
    (28.9,  -28.9,  -24.1),    # mouth corner B (landmark 291)
], dtype=np.float64)
_POSE_LANDMARK_IDX = (1, 152, 33, 263, 61, 291)

# BlazePose 33-point indices used for arm_raise.
_POSE_LEFT_SHOULDER  = 11
_POSE_RIGHT_SHOULDER = 12
_POSE_LEFT_WRIST     = 15
_POSE_RIGHT_WRIST    = 16


# ─────────────────────────────────────────────────────────────────────────
#  Tunable thresholds — every one named and gathered here on purpose so
#  they can be recalibrated without hunting through method bodies.
# ─────────────────────────────────────────────────────────────────────────

HEAD_TILT_ROLL_DEG_MIN       = 14.0   # head "tilt" (ear-to-shoulder) angle
HEAD_NOD_PITCH_DEG_MIN       = 16.0   # head "nod" (chin up/down) angle
HEAD_ROTATION_YAW_DEG_MIN    = 20.0   # head "turn left/right" angle
HEAD_LOOK_GAZE_OFFSET_MIN    = 0.15   # iris ratio distance from center (0.5)
HEAD_LOOK_MAX_YAW_DEG        = 12.0   # keeps "look" distinct from "rotation":
                                      # head must stay roughly forward while
                                      # only the eyes move
ARM_RAISE_WRIST_MARGIN       = 0.06   # normalized-y wrist must clear the
                                      # shoulder line by at least this much
                                      # (smaller y = higher on screen)

_CV_VALIDATED_EXERCISES = frozenset({
    'head_tilt', 'head_nod', 'head_rotation', 'head_look', 'arm_raise',
})
_TIMED_ONLY_EXERCISES = frozenset({'leg_raise', 'heel_raise'})


def is_cv_validatable(exercise_id: str) -> bool:
    return exercise_id in _CV_VALIDATED_EXERCISES


@dataclass
class _HeadPose:
    yaw: float
    pitch: float
    roll: float
    gaze_x: float
    gaze_y: float


class ExercisePoseMonitor:
    """
    Stateless-per-call pose evaluator. One instance is shared across an
    entire Unplug session (created lazily, closed once at the end) —
    creating/destroying MediaPipe Tasks objects per exercise would be
    wasteful and adds latency right when responsiveness matters most.
    """

    def __init__(self):
        self._face_landmarker = None
        self._pose_landmarker = None
        self._available = False
        self._init_error: Optional[str] = None
        self._t0 = time.time()
        self._last_ts_ms = -1
        self._try_init()

    @property
    def available(self) -> bool:
        return self._available

    def _try_init(self):
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import vision as mp_vision
            from mediapipe.tasks.python.core.base_options import BaseOptions
            from mediapipe.tasks.python.vision.core.vision_task_running_mode import (
                VisionTaskRunningMode,
            )

            # Prefer the copy Horizon Mode already ships (avoids a
            # redundant ~5MB download), falling back to the same
            # on-demand downloader core/detector.py uses.
            import os
            here = os.path.dirname(os.path.abspath(__file__))
            bundled_face_model = os.path.join(
                here, '..', 'ui', 'horizon', 'models', 'face_landmarker.task')
            bundled_face_model = os.path.normpath(bundled_face_model)
            face_model_path = (
                bundled_face_model if os.path.isfile(bundled_face_model)
                else _download_model(_FACE_MODEL_URL, 'face_landmarker.task')
            )
            pose_model_path = _download_model(_POSE_MODEL_URL, 'pose_landmarker_lite.task')

            if not face_model_path or not pose_model_path:
                self._init_error = 'model file(s) unavailable (no cached copy, no network)'
                return

            self._mp = mp
            self._running_mode = VisionTaskRunningMode

            self._face_landmarker = mp_vision.FaceLandmarker.create_from_options(
                mp_vision.FaceLandmarkerOptions(
                    base_options=BaseOptions(model_asset_path=face_model_path),
                    running_mode=VisionTaskRunningMode.VIDEO,
                    num_faces=1,
                    min_face_detection_confidence=0.5,
                    min_tracking_confidence=0.5,
                ))
            self._pose_landmarker = mp_vision.PoseLandmarker.create_from_options(
                mp_vision.PoseLandmarkerOptions(
                    base_options=BaseOptions(model_asset_path=pose_model_path),
                    running_mode=VisionTaskRunningMode.VIDEO,
                    num_poses=1,
                    min_pose_detection_confidence=0.5,
                    min_pose_presence_confidence=0.5,
                    min_tracking_confidence=0.5,
                ))
            self._available = True
        except Exception as e:
            self._init_error = repr(e)
            self._available = False

    def close(self):
        for obj in (self._face_landmarker, self._pose_landmarker):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self._face_landmarker = None
        self._pose_landmarker = None
        self._available = False

    def _next_timestamp_ms(self) -> int:
        ts = int((time.time() - self._t0) * 1000)
        if ts <= self._last_ts_ms:
            ts = self._last_ts_ms + 1
        self._last_ts_ms = ts
        return ts

    # ── Public API ───────────────────────────────────────────────────

    def evaluate(self, exercise_id: str, bgr_frame: np.ndarray) -> Optional[bool]:
        """
        Returns True (pose matches the exercise), False (a face/pose
        WAS detected but doesn't match yet), or None (inconclusive —
        no face/pose detected this frame, or the monitor isn't
        available at all). Never raises.
        """
        if exercise_id in _TIMED_ONLY_EXERCISES:
            raise ValueError(
                f'"{exercise_id}" is not CV-validated by design — see module '
                'docstring. Use the timed/predetermined progression instead.')
        if not self._available:
            return None
        try:
            if exercise_id == 'arm_raise':
                return self._check_arm_raise(bgr_frame)
            return self._check_head_exercise(exercise_id, bgr_frame)
        except Exception:
            # Never let a landmark-math edge case (e.g. a degenerate
            # frame) escape into the camera thread.
            return None

    # ── Head exercises (face landmarker + solvePnP + iris gaze) ────────

    def _get_head_pose(self, bgr_frame: np.ndarray) -> Optional[_HeadPose]:
        h, w = bgr_frame.shape[:2]
        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        mp_image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self._face_landmarker.detect_for_video(mp_image, self._next_timestamp_ms())
        if not result.face_landmarks:
            return None
        lm = result.face_landmarks[0]

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
        ok, rvec, _ = cv2.solvePnP(
            _MODEL_POINTS_3D, image_points, camera_matrix, np.zeros((4, 1)),
            flags=cv2.SOLVEPNP_ITERATIVE,
        )
        if not ok:
            return None
        rmat, _ = cv2.Rodrigues(rvec)
        angles, _, _, _, _, _ = cv2.RQDecomp3x3(rmat)
        pitch, yaw, roll = angles
        # Same ambiguity-correction as horizon_gaze.py's _head_forward.
        if pitch > 90:
            pitch = 180 - pitch
        elif pitch < -90:
            pitch = -180 - pitch

        gx, gy = self._gaze_ratio(lm)
        return _HeadPose(yaw=yaw, pitch=pitch, roll=roll, gaze_x=gx, gaze_y=gy)

    @staticmethod
    def _gaze_ratio(lm) -> Tuple[float, float]:
        def ratio_for(eye_idx, iris_idx):
            outer, inner, top, bottom = (np.array([lm[i].x, lm[i].y]) for i in eye_idx)
            iris_c = np.array([[lm[i].x, lm[i].y] for i in iris_idx]).mean(axis=0)
            ex0, ex1 = sorted([outer[0], inner[0]])
            ey0, ey1 = sorted([top[1], bottom[1]])
            gx = (iris_c[0] - ex0) / (ex1 - ex0) if (ex1 - ex0) > 1e-6 else 0.5
            gy = (iris_c[1] - ey0) / (ey1 - ey0) if (ey1 - ey0) > 1e-6 else 0.5
            return gx, gy

        gx_a, gy_a = ratio_for(_EYE_A, _IRIS_A)
        gx_b, gy_b = ratio_for(_EYE_B, _IRIS_B)
        return (gx_a + gx_b) / 2.0, (gy_a + gy_b) / 2.0

    def _check_head_exercise(self, exercise_id: str, bgr_frame: np.ndarray) -> Optional[bool]:
        pose = self._get_head_pose(bgr_frame)
        if pose is None:
            return None

        if exercise_id == 'head_tilt':
            return abs(pose.roll) >= HEAD_TILT_ROLL_DEG_MIN
        if exercise_id == 'head_nod':
            return abs(pose.pitch) >= HEAD_NOD_PITCH_DEG_MIN
        if exercise_id == 'head_rotation':
            return abs(pose.yaw) >= HEAD_ROTATION_YAW_DEG_MIN
        if exercise_id == 'head_look':
            gaze_offset = max(abs(pose.gaze_x - 0.5), abs(pose.gaze_y - 0.5))
            return (gaze_offset >= HEAD_LOOK_GAZE_OFFSET_MIN
                    and abs(pose.yaw) <= HEAD_LOOK_MAX_YAW_DEG)

        # Unknown id reaching here would be a call-site bug, not a user
        # problem — surface it as inconclusive rather than raising out
        # of the camera thread.
        return None

    # ── arm_raise (pose landmarker, shoulder/wrist geometry) ───────────

    def _check_arm_raise(self, bgr_frame: np.ndarray) -> Optional[bool]:
        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        mp_image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self._pose_landmarker.detect_for_video(mp_image, self._next_timestamp_ms())
        if not result.pose_landmarks:
            return None
        plm = result.pose_landmarks[0]
        ls, rs = plm[_POSE_LEFT_SHOULDER], plm[_POSE_RIGHT_SHOULDER]
        lw, rw = plm[_POSE_LEFT_WRIST], plm[_POSE_RIGHT_WRIST]
        shoulder_y = (ls.y + rs.y) / 2.0
        # normalized y: smaller = higher on screen. Either arm raised
        # above the shoulder line by the margin counts.
        left_up  = (shoulder_y - lw.y) >= ARM_RAISE_WRIST_MARGIN
        right_up = (shoulder_y - rw.y) >= ARM_RAISE_WRIST_MARGIN
        return bool(left_up or right_up)


# ─────────────────────────────────────────────────────────────────────────
#  AttemptTracker — turns a stream of per-frame True/False/None verdicts
#  into the UX behavior the spec asks for:
#    - a pose must be held briefly before it counts (ignores one lucky
#      frame)
#    - a few encouraging nudges while the user keeps trying
#    - a hard timeout so a struggling/undetected user is never stuck
#
#  Feedback wording is deliberately a plain 3-state mapping, not a
#  free-form rotation, so what gets shown always accurately reflects
#  what the camera actually saw:
#    - CORRECT exercise detected (held for HOLD_CONFIRM_SEC)  -> "Great job!"
#    - the FIRST nudge, i.e. the user is still mid-attempt and simply
#      hasn't been detected performing it correctly yet          -> "Keep going!"
#      (motivational, not corrective — we don't want to sound like
#      they're doing something wrong on the very first check-in)
#    - every nudge AFTER that, i.e. the exercise is still not being
#      detected/performed correctly after a real chance to do it   -> "Try again"
#      (an explicit, corrective ask — the pose genuinely wasn't
#      recognized, so say so plainly rather than staying vague)
#    - giving up and moving on after MAX_RETRY_NUDGES/HARD_TIMEOUT_SEC
#      -> "Keep going!" again (encouraging, not a scolding note to end
#      on, since the routine is about to move past them regardless)
#    - a correct pose that hasn't been held long enough yet          -> "Hold it!"
#      (distinct from all of the above: not a verdict, just "you're
#      doing it, stay there a moment longer")
# ─────────────────────────────────────────────────────────────────────────

HOLD_CONFIRM_SEC        = 0.6   # how long a matching pose must be held
ATTEMPT_WINDOW_SEC      = 3.0   # time between encouragement nudges
MAX_RETRY_NUDGES        = 2     # nudges before giving up on this exercise
HARD_TIMEOUT_SEC        = 11.0  # absolute cap regardless of nudge count
FEEDBACK_MIN_INTERVAL_SEC = 1.2  # never show feedback more often than this

_HOLD_FEEDBACK        = 'Hold it!'       # correct pose, still holding to confirm
_SUCCESS_FEEDBACK     = 'Great job!'     # exercise correctly detected
_MOTIVATION_FEEDBACK  = 'Keep going!'    # first nudge — encouragement, not correction
_RETRY_FEEDBACK       = 'Try again'      # later nudges — genuinely not detected/performed
_TIMEOUT_FEEDBACK     = 'Keep going!'    # giving up on this one and moving on


@dataclass
class _TrackerResult:
    status: str                      # 'pending' | 'correct' | 'proceed_timeout'
    feedback: Optional[str] = None   # None = nothing new to show this tick
    attempts: int = 0


class AttemptTracker:
    def __init__(self, exercise_id: str):
        self.exercise_id = exercise_id
        self._start = time.time()
        self._window_start = self._start
        self._hold_start: Optional[float] = None
        self._nudges = 0
        self._last_feedback_at = 0.0
        self._told_hold = False

    def feed(self, is_match: Optional[bool]) -> _TrackerResult:
        now = time.time()

        if is_match:
            if self._hold_start is None:
                self._hold_start = now
            held_for = now - self._hold_start
            if held_for >= HOLD_CONFIRM_SEC:
                # Correct exercise detected -> tell them plainly.
                return _TrackerResult('correct', _SUCCESS_FEEDBACK, self._nudges)
            if not self._told_hold and self._can_speak(now):
                self._told_hold = True
                self._last_feedback_at = now
                return _TrackerResult('pending', _HOLD_FEEDBACK, self._nudges)
            return _TrackerResult('pending', None, self._nudges)
        else:
            # Not a match this frame — the camera isn't currently
            # seeing the exercise being performed. Not a verdict on
            # its own (a single missed frame is normal/expected), but
            # it does mean any in-progress hold no longer counts.
            self._hold_start = None
            self._told_hold = False

        if now - self._start >= HARD_TIMEOUT_SEC or self._nudges > MAX_RETRY_NUDGES:
            return _TrackerResult('proceed_timeout', _TIMEOUT_FEEDBACK, self._nudges)

        if now - self._window_start >= ATTEMPT_WINDOW_SEC:
            self._window_start = now
            self._nudges += 1
            if self._can_speak(now):
                # First nudge: still early, stay encouraging. Every
                # nudge after that: the exercise genuinely hasn't been
                # detected across a real window of time, so say so
                # directly rather than staying softly ambiguous.
                fb = _MOTIVATION_FEEDBACK if self._nudges == 1 else _RETRY_FEEDBACK
                self._last_feedback_at = now
                return _TrackerResult('pending', fb, self._nudges)

        return _TrackerResult('pending', None, self._nudges)

    def _can_speak(self, now: float) -> bool:
        return (now - self._last_feedback_at) >= FEEDBACK_MIN_INTERVAL_SEC

    @property
    def elapsed(self) -> float:
        return time.time() - self._start

