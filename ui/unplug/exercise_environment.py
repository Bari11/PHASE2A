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
"""

import functools
import http.server
import threading
from pathlib import Path

from PyQt5.QtWidgets import QWidget, QVBoxLayout, QApplication
from PyQt5.QtCore import Qt, QTimer, QUrl, QObject, pyqtSignal, pyqtSlot
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWebChannel import QWebChannel

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent   # canary/
VIEWER_HTML_REL = "ui/unplug/viewer.html"

# Real total: intro idle + 7 exercises (real authored clip durations,
# ~33.3s combined) + between-exercise idles + final idle ≈ 45s. This
# safety net gives real margin above that in case a frame or two runs
# slow, without letting a genuinely broken scene hang forever.
SAFETY_NET_MS = 60_000


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
    """The only thing exposed to JS via QWebChannel — one signal."""
    session_complete_signal = pyqtSignal()

    @pyqtSlot()
    def session_complete(self):
        self.session_complete_signal.emit()


class ExerciseEnvironment(QWidget):
    """
    Full-screen host for the Three.js wellness environment.

    animation_done fires once (and only once) — either because the JS
    scheduler genuinely finished, or because the safety-net timer fired
    first. Either way, the caller can safely move on to the closing
    animation.
    """

    animation_done = pyqtSignal()

    def __init__(self, character: str = 'boy', parent=None):
        super().__init__(parent)
        self._character = 'girl' if character == 'girl' else 'boy'
        self._finished = False

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

        self._bridge = _Bridge(self)
        self._bridge.session_complete_signal.connect(self._on_session_complete)

        self._channel = QWebChannel(self._view.page())
        self._channel.registerObject('bridge', self._bridge)
        self._view.page().setWebChannel(self._channel)

        self._safety_timer = QTimer(self)
        self._safety_timer.setSingleShot(True)
        self._safety_timer.timeout.connect(self._on_session_complete)

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

    def _on_session_complete(self):
        if self._finished:
            return
        self._finished = True
        self._safety_timer.stop()
        self.hide()
        self.animation_done.emit()

    def closeEvent(self, event):
        self._safety_timer.stop()
        super().closeEvent(event)


if __name__ == '__main__':
    import sys
    app = QApplication(sys.argv)
    env = ExerciseEnvironment(character='boy')
    env.animation_done.connect(lambda: (print('Exercise session done'), app.quit()))
    env.start()
    sys.exit(app.exec_())