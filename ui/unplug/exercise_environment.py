"""
exercise_environment.py — Canary Unplug Mode: 3D Wellness Environment
================================================================
Hosts the Three.js scene (ui/unplug/viewer.html) inside an opaque,
frameless QWebEngineView. Plays between the Opening Animation and the
Closing Animation.

Much simpler than the old break_screen.py it replaces: there's no
QPainter-drawn background or side panels here — the environment, the
character, and the exercise-name/progress overlay are all rendered
inside the Three.js scene itself (see viewer.html's OverlayUI). This
widget's only jobs are:
  1. Host the WebEngine view, sized to the screen.
  2. Tell it which character to load (?character=boy|girl in the URL,
     read from settings).
  3. Listen for the one signal JS sends back — "the exercise sequence
     is finished" — via a small QWebChannel bridge, and re-emit that
     as `animation_done` for the caller.

A safety-net timer force-finishes the session if that signal never
arrives (e.g. a WebGL context loss or a script error), so a broken
scene can't hang Unplug Mode indefinitely.

Why viewer.html is served over http://127.0.0.1 instead of file://
--------------------------------------------------------------------
GLTFLoader (and every other Three.js loader) fetches its resources
with the Fetch API. Chromium's fetch() implementation simply doesn't
support the "file" scheme at all — this isn't a permissions setting
that can be toggled, it's a protocol Chromium never implemented fetch
support for. So even though viewer.html itself loads fine from a
file:// URL, its own attempt to `fetch()` environment.glb / boy.glb /
girl.glb fails with "URL scheme 'file' is not supported" regardless of
any Qt-side settings. (three.bundle.js loads fine via file:// because
`<script src="...">` doesn't go through fetch().)

The fix: run a tiny local HTTP server (below) and load viewer.html —
and therefore every relative asset path inside it — over
http://127.0.0.1:<port>/... instead. Nothing in viewer.html needed to
change for this; its relative paths (../../assets/...) resolve
identically whether the page itself was served via file:// or http://.

Usage
-----
    env = ExerciseEnvironment(character='boy')
    env.animation_done.connect(on_exercises_finished)
    env.start()

Phase 2 addition — exercise validation
---------------------------------------
This widget now also owns an ExerciseValidationCoordinator (see
exercise_validation_coordinator.py), which taps into the same camera
frame_hook mechanism Horizon Mode already uses to:
  - stream a small live preview back into viewer.html
  - run CV pose validation for the 5 exercises that support it
  - collect a per-exercise attempt log for Phase 4

None of this touches the opening/closing animations or how the
character/environment/exercise animations themselves render — it's
additive, wired through a few new QWebChannel signals/slots on
_Bridge (validation_update, preview_frame, camera_status,
start_exercise_validation, stop_exercise_validation, log_attempt).

Pass `shared_detector=` a running StressPostureDetector if the caller
already has one (so a single camera device stays shared); otherwise
the coordinator creates and owns a temporary one for the session.
"""

import functools
import http.server
import threading
from pathlib import Path
from typing import Optional

from PyQt5.QtWidgets import QWidget, QVBoxLayout, QApplication
from PyQt5.QtCore import Qt, QTimer, QUrl, QObject, pyqtSignal, pyqtSlot
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWebChannel import QWebChannel

from ui.unplug.exercise_validation_coordinator import ExerciseValidationCoordinator

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent   # canary/
VIEWER_HTML_REL = "ui/unplug/viewer.html"

# Base timeline (no CV retries needed): intro idle + 7 authored exercise
# clips (~33.3s combined) + between-exercise idles + final idle. On top
# of that, the 5 CV-validated exercises can each run up to
# AttemptTracker.HARD_TIMEOUT_SEC (core/exercise_pose_monitor.py) before
# giving up and moving on — so the worst case (every CV exercise times
# out fully) is meaningfully longer than the happy-path duration. This
# safety net is sized above THAT worst case, not just the happy path,
# so a genuinely broken scene is still caught without ever cutting off
# a real (if slow) validation session.
SAFETY_NET_MS = 100_000


class _LocalAssetServer:
    """
    Tiny local HTTP server serving the whole project root, so
    viewer.html's fetch() calls for .glb files work (see module
    docstring for why file:// can't do this). Started once, lazily, on
    a daemon thread — shared across every ExerciseEnvironment instance
    for the lifetime of the app.
    """
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler,
            directory=str(PROJECT_ROOT),
        )
        # port=0 -> OS assigns a free local port
        self._httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.port = self._httpd.server_address[1]
        thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        thread.start()

    @classmethod
    def get(cls) -> '_LocalAssetServer':
        with cls._lock:
            if cls._instance is None:
                cls._instance = _LocalAssetServer()
        return cls._instance


class _Bridge(QObject):
    """
    Exposed to JS via QWebChannel. Originally just session_complete;
    Phase 2 adds the exercise-validation channel (two directions):

      JS  -> Python : start_exercise_validation(id), stop_exercise_validation(),
                      log_attempt(json_string)
      Python -> JS  : validation_update(id, status, feedback),
                      preview_frame(base64_jpeg), camera_status(status)

    Python->JS signals work the same way as any other pyqtSignal on a
    QWebChannel-registered QObject: JS subscribes with
    `bridge.validation_update.connect(fn)`, exactly like a native JS
    event emitter.
    """
    session_complete_signal = pyqtSignal()
    validation_update = pyqtSignal(str, str, str)   # exercise_id, status, feedback
    preview_frame = pyqtSignal(str)                 # base64 JPEG, no data: prefix
    camera_status = pyqtSignal(str)                 # 'available' | 'unavailable'

    def __init__(self, coordinator: 'ExerciseValidationCoordinator', parent=None):
        super().__init__(parent)
        self._coordinator = coordinator

    @pyqtSlot()
    def session_complete(self):
        self.session_complete_signal.emit()

    @pyqtSlot(str)
    def start_exercise_validation(self, exercise_id: str):
        self._coordinator.start_exercise_validation(exercise_id)

    @pyqtSlot()
    def stop_exercise_validation(self):
        self._coordinator.stop_exercise_validation()

    @pyqtSlot(str)
    def log_attempt(self, json_str: str):
        import json
        try:
            record = json.loads(json_str)
        except Exception:
            return
        self._coordinator.log_attempt(record)


class ExerciseEnvironment(QWidget):
    """
    Full-screen host for the Three.js wellness environment.

    animation_done fires once (and only once) — either because the JS
    scheduler genuinely finished, or because the safety-net timer fired
    first. Either way, the caller can safely move on to the closing
    animation.
    """

    animation_done = pyqtSignal()

    # How long to wait for the first preview frame before telling
    # viewer.html the camera has genuinely failed to show up (as
    # opposed to still being probed) — see camera_status('opening') in
    # ExerciseValidationCoordinator.start(), which fires immediately so
    # the preview panel always shows something rather than sitting
    # blank during this whole wait either way.
    #
    # Was 4000ms — too short. StressPostureDetector's own camera-open
    # step (core/detector.py's _loop) tries up to 4 device indices in
    # sequence with cv2.VideoCapture, and on some Windows driver/backend
    # combinations a single failed/slow index probe alone can take
    # several seconds before falling through to the next one — a real,
    # externally-imposed hardware/OS delay, not something fixable from
    # this file. 4s was routinely shorter than that, so this fired
    # 'unavailable' as a false negative WHILE the camera was still
    # mid-probe and about to succeed a moment later — exactly matching
    # "camera starts late, exercise already begins" (the exercise
    # routine treats 'unavailable' as license to proceed, same as a
    # real frame arriving — see cameraReadyPromise in viewer.html).
    # 12s gives that probe realistic room to finish on slower hardware
    # before this gives up and calls it genuinely unavailable.
    CAMERA_STATUS_GRACE_MS = 12_000

    def __init__(self, character: str = 'boy', parent=None, shared_detector=None):
        super().__init__(parent)
        self._character = 'girl' if character == 'girl' else 'boy'
        self._finished = False
        self.last_session_log = None   # populated once the session ends

        self._coordinator = ExerciseValidationCoordinator(
            bridge=None,  # set right after _bridge is constructed below
            character=self._character,
            shared_detector=shared_detector,
        )

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        # Opaque, not translucent — WA_TranslucentBackground on
        # QWebEngineView specifically is unreliable across Qt/driver
        # versions (this was the actual cause of the "black screen,
        # nothing ever shows" symptom). The scene doesn't need
        # transparency anyway: it has its own full-screen baked sky, so
        # there's nothing underneath that needs to show through. A solid
        # black QWidget background also means the screen stays opaque
        # black continuously from the Opening Animation's fade-out
        # through this widget's page load — no flash back to the desktop
        # in between.
        self.setStyleSheet('background-color: black;')
        self.setAutoFillBackground(True)

        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._view = QWebEngineView(self)
        self._view.page().setBackgroundColor(Qt.black)
        layout.addWidget(self._view)

        self._bridge = _Bridge(self._coordinator, self)
        self._coordinator._bridge = self._bridge
        self._bridge.session_complete_signal.connect(self._on_session_complete)

        self._channel = QWebChannel(self._view.page())
        self._channel.registerObject('bridge', self._bridge)
        self._view.page().setWebChannel(self._channel)

        self._safety_timer = QTimer(self)
        self._safety_timer.setSingleShot(True)
        self._safety_timer.timeout.connect(self._on_session_complete)

        # If no preview frame has arrived shortly after start (no
        # camera, permission denied, device busy, etc.), tell
        # viewer.html explicitly rather than leaving the preview panel
        # blank with no explanation. A real preview frame arriving
        # later still flips it back via camera_status('available') —
        # see ExerciseValidationCoordinator._emit_preview.
        self._camera_grace_timer = QTimer(self)
        self._camera_grace_timer.setSingleShot(True)
        self._camera_grace_timer.timeout.connect(self._check_camera_grace)

    # ── Public API ──────────────────────────────────────────────

    def start(self):
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)
        self._finished = False

        # Show/raise BEFORE calling load(). QWebEngineView's first paint
        # needs the widget to already be mapped/visible on screen — if
        # load() starts while the widget is still hidden and show() only
        # comes after, Chromium can render its first frame into a surface
        # that never gets flushed to the screen until something else (any
        # unrelated window/compositor event — even pressing the Windows
        # key) forces a repaint. That's the exact "white/blank until I
        # touch Windows" symptom. Showing first gives it a real, already-
        # visible surface to paint into from frame one.
        self.show()
        self.raise_()
        # activateWindow() + setFocus(): without these, this Qt.Tool /
        # WindowStaysOnTopHint window can sit visibly on top without
        # actually holding OS-level keyboard focus, and even when the
        # top-level widget IS focused, the embedded QWebEngineView still
        # needs its OWN explicit focus for keydown events to reach the
        # page's JavaScript at all (Chromium is a separate input target
        # from the Qt widget tree around it). Concretely, this is why
        # the spacebar-x3 early-exit listener in viewer.html was never
        # firing — window.addEventListener('keydown', ...) inside the
        # page simply never received anything.
        self.activateWindow()
        self._view.setFocus()
        # Chromium can reset/steal focus once its own page content
        # finishes initializing, undoing the setFocus() above — so
        # re-assert it once loading completes, not just before it starts.
        self._view.loadFinished.connect(self._reassert_focus)

        self._coordinator.start()
        self._camera_grace_timer.start(self.CAMERA_STATUS_GRACE_MS)

        viewer_path = PROJECT_ROOT / VIEWER_HTML_REL
        if viewer_path.exists():
            server = _LocalAssetServer.get()
            url = QUrl(f'http://127.0.0.1:{server.port}/{VIEWER_HTML_REL}')
            url.setQuery(f'character={self._character}')
            self._view.load(url)
        else:
            print(f'[Canary] viewer.html not found at {viewer_path}')
            self._on_session_complete()
            return

        # Cheap, harmless insurance on top of the reordering above: nudge
        # a repaint shortly after load starts, in case the platform still
        # needs an explicit poke. update() is a no-op if the view is
        # already painting normally.
        QTimer.singleShot(150, self._view.update)

        self._safety_timer.start(SAFETY_NET_MS)

    # ── Lifecycle ───────────────────────────────────────────────

    def _reassert_focus(self, ok: bool = True):
        # See the comment in start() — Chromium can take focus for
        # itself once the page finishes loading, so this is called
        # again from loadFinished on top of the initial setFocus()
        # call made right after show()/raise_().
        self.activateWindow()
        self._view.setFocus()

    def _check_camera_grace(self):
        if not self._finished and not self._coordinator.camera_available:
            self._bridge.camera_status.emit('unavailable')

    def _on_session_complete(self):
        if self._finished:
            return
        self._finished = True
        self._safety_timer.stop()
        self._camera_grace_timer.stop()
        self.last_session_log = self._coordinator.finish_session(completed=True)
        self._coordinator.stop()
        # Deliberately NOT self.hide() here — see dismiss() below, same
        # reasoning as OpeningAnimation.dismiss().
        self.animation_done.emit()

    def dismiss(self):
        """
        Call this only AFTER the next full-screen window (typically
        ClosingAnimation) has already been shown and raised on top —
        same handoff pattern as OpeningAnimation.dismiss(), for the
        same reason: hiding this window before the next one is up would
        briefly reveal the desktop underneath.
        """
        self.hide()

    def closeEvent(self, event):
        self._safety_timer.stop()
        self._camera_grace_timer.stop()
        self._coordinator.stop()
        super().closeEvent(event)


if __name__ == '__main__':
    import sys
    app = QApplication(sys.argv)
    env = ExerciseEnvironment(character='boy')

    def _done():
        print('Exercise session done')
        if env.last_session_log is not None:
            print(env.last_session_log.to_json())
        app.quit()

    env.animation_done.connect(_done)
    env.start()
    sys.exit(app.exec_())