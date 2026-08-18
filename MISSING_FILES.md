# ⚠️ Missing File: core/detector.py

This file (`core/detector.py`, the CV detection engine —
`StressPostureDetector`, `SessionData`, etc.) was **not included**
in the files you uploaded for this build, so it could not be placed
in the package.

`ui/tray_app.py` and `main.py` both import from it:

```python
from core.detector import StressPostureDetector, SessionData
```

## To fix

Drop your existing `detector.py` into:

```
canary/core/detector.py
```

Everything else in this zip (tray app with the new Unplug Mode
settings, all Unplug/Horizon Mode screens, viewer.html, exercise
data, etc.) is complete and ready to go — the app will run as soon
as `detector.py` is added back.

---

*(Note: `accuracy3.py` and `debug_detector_view.py` in your upload
were identical files — both appear to be the debug view, not the
detector module — so neither was used as a substitute.)*
