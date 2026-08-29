"""
ui/unplug/exercise_validation_coordinator.py — Unplug Mode CV plumbing
═══════════════════════════════════════════════════════════════════════════
Sits between:
  - the camera (via StressPostureDetector.frame_hook — the SAME
    extension point Horizon Mode already uses; see
    ui/horizon/horizon_gaze.py for precedent),
  - core/exercise_pose_monitor.py (the actual per-exercise geometry),
  - and the Qt/JS bridge in exercise_environment.py, which forwards
    results into viewer.html.

If the caller (ExerciseEnvironment) doesn't already have a running
StressPostureDetector to share, this coordinator creates and owns a
temporary one for the lifetime of the Unplug session — mirroring the
same "own it if nothing else does" pattern tray_app.py already uses
for Horizon Mode (see TrayApp.trigger_horizon_mode). Either way, only
ONE camera device is ever opened.

Also streams a small, throttled JPEG preview of the same frames back
to viewer.html over the bridge (as base64), which is what the on-screen
"live camera preview" actually displays. This deliberately does NOT
use getUserMedia()/<video> in the QWebEngineView: this app's Chromium
build is old enough (see viewer.html's GLTFLoader comment) that camera
permission flows inside QWebEngine are an unnecessary extra risk, and
streaming already-captured frames avoids opening a second camera
device entirely. A few frames per second at ~220px wide is plenty for
a small preview panel and negligible bandwidth over the QWebChannel.
"""

import base64
import time
from typing import Optional

import cv2

from core.exercise_pose_monitor import ExercisePoseMonitor, AttemptTracker, is_cv_validatable
from core.exercise_session_log import UnplugSessionLog, ExerciseAttemptLog


class ExerciseValidationCoordinator:
    # Easy-to-tune preview constants.
    PREVIEW_FRAME_STRIDE  = 2    # further throttle beyond the detector's
                                  # own ~7fps hook rate -> ~3.5fps preview
    PREVIEW_JPEG_QUALITY  = 60
    PREVIEW_MAX_WIDTH_PX  = 220

    # Throttles how often we actually run pose/face inference while an
    # exercise is active, independent of the preview throttle above.
    # Running full landmark inference on every single hooked frame
    # (stacked on top of whatever the shared detector's own analysis
    # already costs, when a monitoring session is sharing the camera)
    # was the concrete cause of the camera feed lagging. HOLD_CONFIRM_SEC
    # (0.6s) and ATTEMPT_WINDOW_SEC (3s) in core/exercise_pose_monitor.py
    # are both generous relative to a single frame interval, so sampling
    # at ~half the hook rate (~3.5fps) loses no real responsiveness.
    EVAL_FRAME_STRIDE = 2

    def __init__(self, bridge, character: str, shared_detector=None):
        self._bridge = bridge
        self._character = character
        self._owns_detector = shared_detector is None
        self._detector = shared_detector
        self._monitor: Optional[ExercisePoseMonitor] = None

        self._active_exercise_id: Optional[str] = None
        self._tracker: Optional[AttemptTracker] = None

        self._preview_frame_n = 0
        self._eval_frame_n = 0
        self._camera_status_sent = False

        self.session_log = UnplugSessionLog(character=character)

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self):
        # Tell viewer.html we're starting immediately, before anything
        # else below — this is what makes the preview panel show
        # "Starting camera…" from frame one instead of sitting blank.
        # The camera-open step a few lines down (StressPostureDetector's
        # own cv2.VideoCapture probe, which tries up to 4 device indices)
        # is a real, sometimes multi-second wait on some hardware/driver
        # combinations — nothing in THIS file can make that faster
        # without touching the shared detector loop every other mode
        # (Calm/Horizon/the main dashboard) also depends on, which isn't
        # something we can verify is safe to change. So instead of
        # trying to shorten that wait, we make sure it's never silent.
        self._bridge.camera_status.emit('opening')

        # Camera FIRST, everything else after. Two real reasons, not
        # just ordering for its own sake:
        #
        #  1. StressPostureDetector.start() only spins up its capture
        #     thread and returns immediately — the actual camera open
        #     + first frames happen on that background thread, so
        #     kicking it off before anything else gets frames (and
        #     therefore the live preview) flowing at the earliest
        #     possible moment, in parallel with the 3D scene loading in
        #     viewer.html rather than waiting behind it.
        #  2. ExercisePoseMonitor() below does synchronous model
        #     loading (reading/downloading the face + pose landmarker
        #     .task files) — that can take a real, noticeable moment on
        #     first run. No exercise can start validating before the
        #     scene finishes loading anyway, so there's no correctness
        #     reason to gate camera startup behind it; doing it after
        #     just means the camera thread — and the preview it feeds —
        #     isn't stuck waiting on model I/O it doesn't need yet.
        if self._owns_detector:
            # Local import: avoids a hard import-time dependency for
            # any caller that only wants the data structures, and
            # matches how tray_app.py already imports it lazily inside
            # trigger_unplug_mode/trigger_horizon_mode.
            from core.detector import StressPostureDetector
            # frame_processing_enabled=False: this detector exists only
            # to give us a camera + frame_hook. Nobody reads its own
            # stress/posture analysis (self._process() in detector.py)
            # during an Unplug session — running it anyway was pure
            # wasted per-frame inference stacked on top of our own
            # validation work, on a single-threaded capture loop. That
            # doubling was the real cause of the camera feed lagging.
            # Only applies when we own a dedicated temporary detector;
            # a shared detector (a monitoring session already running)
            # keeps frame_processing_enabled at its default True, since
            # the dashboard genuinely needs that analysis regardless of
            # Unplug Mode — see _emit_preview/_on_frame's own
            # throttling below for how that case stays light instead.
            self._detector = StressPostureDetector(on_alert=None, frame_processing_enabled=False)
            self._detector.start()

        if self._detector is not None:
            self._detector.frame_hook = self._on_frame

        self._monitor = ExercisePoseMonitor()

    def stop(self):
        if self._detector is not None:
            self._detector.frame_hook = None
            if self._owns_detector:
                try:
                    self._detector.stop()
                except Exception:
                    pass
        if self._monitor is not None:
            self._monitor.close()
            self._monitor = None

    def finish_session(self, completed: bool) -> UnplugSessionLog:
        self.session_log.finish(completed=completed)
        return self.session_log

    @property
    def camera_available(self) -> bool:
        return self._camera_status_sent

    # ── JS → Python (forwarded from _Bridge slots) ──────────────────

    def start_exercise_validation(self, exercise_id: str):
        if not is_cv_validatable(exercise_id):
            # Timed exercises (leg_raise, heel_raise) never go through
            # CV — see core/exercise_pose_monitor.py's module docstring.
            self._active_exercise_id = None
            self._tracker = None
            return
        self._active_exercise_id = exercise_id
        self._tracker = AttemptTracker(exercise_id)
        self._eval_frame_n = 0

    def stop_exercise_validation(self):
        self._active_exercise_id = None
        self._tracker = None

    def log_attempt(self, record: dict):
        self.session_log.add(ExerciseAttemptLog(
            exercise=str(record.get('exercise', '')),
            label=str(record.get('label', '')),
            cv_validated=bool(record.get('cvValidated', False)),
            result=str(record.get('result', '')),
            duration_sec=float(record.get('durationSec', 0.0) or 0.0),
            feedback=str(record.get('feedback', '')),
            attempts=int(record.get('attempts', 0) or 0),
        ))

    # ── Camera thread callback (StressPostureDetector.frame_hook) ────
    # Runs on the detector's OWN background thread, NOT the Qt GUI
    # thread. Emitting Qt signals here is safe — PyQt automatically
    # queues a cross-thread signal emission onto the receiving
    # object's (the GUI thread's) event loop, the same as any other
    # queued connection.

    def _on_frame(self, bgr_frame):
        self._emit_preview(bgr_frame)

        if self._active_exercise_id is None or self._tracker is None:
            return

        # Throttle inference frequency, not the tracker's timing state —
        # skipped frames simply aren't fed to the tracker at all, so a
        # skipped frame can never reset an in-progress hold or otherwise
        # disturb HOLD_CONFIRM_SEC/ATTEMPT_WINDOW_SEC's wall-clock logic.
        self._eval_frame_n += 1
        if self._eval_frame_n % self.EVAL_FRAME_STRIDE != 0:
            return

        verdict = self._monitor.evaluate(self._active_exercise_id, bgr_frame)
        result = self._tracker.feed(verdict)

        if result.feedback is not None:
            self._bridge.validation_update.emit(
                self._active_exercise_id, result.status, result.feedback)

        if result.status in ('correct', 'proceed_timeout'):
            self._active_exercise_id = None
            self._tracker = None

    def _emit_preview(self, bgr_frame):
        self._preview_frame_n += 1
        if self._preview_frame_n % self.PREVIEW_FRAME_STRIDE != 0:
            return
        try:
            h, w = bgr_frame.shape[:2]
            if w > self.PREVIEW_MAX_WIDTH_PX:
                scale = self.PREVIEW_MAX_WIDTH_PX / float(w)
                small = cv2.resize(bgr_frame, (self.PREVIEW_MAX_WIDTH_PX, max(1, int(h * scale))))
            else:
                small = bgr_frame
            ok, buf = cv2.imencode(
                '.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, self.PREVIEW_JPEG_QUALITY])
            if not ok:
                return
            b64 = base64.b64encode(buf).decode('ascii')
            self._bridge.preview_frame.emit(b64)
            if not self._camera_status_sent:
                self._camera_status_sent = True
                self._bridge.camera_status.emit('available')
        except Exception:
            # A dropped preview frame is cosmetic only — never let it
            # affect exercise validation or take down the camera loop.
            pass
