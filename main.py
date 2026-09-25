#!/usr/bin/env python3
"""
main.py — Canary 🐦  Stress & Posture Awareness System
───────────────────────────────────────────────────────
Launches the PyQt5 system-tray application.

Usage:
    python3 main.py

Requirements:
    pip install PyQt5 opencv-python numpy mediapipe

Voice (optional — any ONE of these):
    Windows : built-in (PowerShell SAPI)
    macOS   : built-in (say)
    Linux   : sudo apt install espeak   OR   sudo apt install festival
"""

import sys
import os
from PyQt5.QtCore import Qt, QCoreApplication

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

from PyQt5.QtWebEngineWidgets import QWebEngineView

# Ensure user site-packages is on path (e.g. when run from an IDE)



# ── Dependency check ──────────────────────────────────────────────────────────

def check_deps():
    for pkg, import_name in [
        ('PyQt5', 'PyQt5'),
        ('opencv-python', 'cv2'),
        ('numpy', 'numpy'),
        ('mediapipe', 'mediapipe'),
    ]:
        print(f"Checking {import_name}...", end=" ")
        try:
            __import__(import_name)
            print("OK")
        except Exception:
            import traceback
            print("FAILED")
            traceback.print_exc()
            raise


check_deps()


# ── Launch ────────────────────────────────────────────────────────────────────

from PyQt5.QtWidgets import QApplication, QSystemTrayIcon
from PyQt5.QtCore    import Qt
from PyQt5.QtGui     import QIcon

QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps,    True)

_APP_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'assets', 'logo_icon.png')


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('Canary')
    app.setQuitOnLastWindowClosed(False)   # keep alive in tray
    if os.path.isfile(_APP_ICON_PATH):
        app.setWindowIcon(QIcon(_APP_ICON_PATH))

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print('ERROR: No system tray found on this platform.')
        sys.exit(1)

    sys.path.insert(0, os.path.dirname(__file__))
    from ui.tray_app import TrayApp

    tray = TrayApp(app)   # noqa: F841

    print('╔════════════════════════════════════════════════════════╗')
    print('║   🐦  Canary is running in the system tray            ║')
    print('║   → Left-click tray icon  : Open Dashboard           ║')
    print('║   → Right-click tray icon : Start / Stop / Quit      ║')
    print('║   → Alerts fire only on posture/stress CHANGES       ║')
    print('║   → Camera blocked for 8 s → orange camera alert     ║')
    print('╚════════════════════════════════════════════════════════╝')

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
